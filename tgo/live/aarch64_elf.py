#!/usr/bin/env python3
"""AArch64 ELF64与mapping-symbol只读解析助手，源自上一轮冻结工具。"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import struct
from tgo.elf import ELF as CheckedELF

SCHEMA = 'runtime-plt-direct-plan-v1'
EXPERIMENT_ENV = 'RUNTIME_PLT_EXPERIMENT_TOKEN'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sx(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def branch_target(word, pc):
    if word & 0xfc000000 not in (0x94000000, 0x14000000):
        return None
    return pc + sx(word & 0x3ffffff, 26) * 4


def branch_word(opcode, pc, target):
    delta = target - pc
    if opcode not in ('BL', 'B') or delta % 4 or not -(1 << 27) <= delta < (1 << 27):
        raise ValueError('branch opcode/alignment/range')
    return (0x94000000 if opcode == 'BL' else 0x14000000) | ((delta // 4) & 0x3ffffff)


def parse_maps(text):
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        start, end = (int(x, 16) for x in fields[0].split('-'))
        rows.append(dict(start=start, end=end, perms=fields[1], offset=int(fields[2], 16),
                         device=fields[3], inode=int(fields[4]), path=fields[5] if len(fields) > 5 else ''))
    return rows


def read_starttime(pid):
    # comm允许空格/括号；starttime是右括号后的第20个字段。
    text = Path(f'/proc/{pid}/stat').read_text()
    return int(text[text.rfind(')') + 2:].split()[19])


class ELF:
    def __init__(self, path):
        self.path = Path(path)
        checked=CheckedELF(path)
        self.data=checked.data;h=checked.header
        if h[2] != 183 or h[1] != 3:
            raise ValueError('only AArch64 ET_DYN supported')
        if h[11] < 64 or h[12] == 0 or h[13] >= h[12]:
            raise ValueError('invalid ELF sections')
        self.sections = []
        for i in range(h[12]):
            v = struct.unpack_from('<IIQQQQIIQQ', self.data, h[6] + i * h[11])
            row = dict(index=i, name_offset=v[0], type=v[1], flags=v[2], addr=v[3],
                       offset=v[4], size=v[5], link=v[6], info=v[7], align=v[8], entsize=v[9])
            if row['type'] != 8 and row['offset'] + row['size'] > len(self.data):
                raise ValueError('section outside file')
            self.sections.append(row)
        names = self.blob(self.sections[h[13]])
        for row in self.sections:
            row['name'] = self.string(names, row['name_offset'])
        self.by_name = {s['name']: s for s in self.sections}
        self.loads = []
        for i in range(h[10]):
            v = struct.unpack_from('<IIQQQQQQ', self.data, h[5] + i * h[9])
            if v[0] == 1:
                self.loads.append(dict(flags=v[1], offset=v[2], vaddr=v[3], filesz=v[5], memsz=v[6]))
        self.symbols = []
        self.tables = {}
        for section in self.sections:
            if section['type'] not in (2, 11):
                continue
            if section['entsize'] != 24 or section['size'] % 24 or section['link'] >= len(self.sections):
                raise ValueError('invalid symbol table')
            strings = self.blob(self.sections[section['link']])
            symbols = []
            for off in range(section['offset'], section['offset'] + section['size'], 24):
                v = struct.unpack_from('<IBBHQQ', self.data, off)
                symbol = dict(name=self.string(strings, v[0]), type=v[1] & 15, binding=v[1] >> 4,
                              other=v[2], shndx=v[3], value=v[4], size=v[5])
                symbols.append(symbol)
            self.tables[section['index']] = symbols
            self.symbols.extend(symbols)

    @staticmethod
    def string(data, offset):
        if offset >= len(data):
            raise ValueError('string offset outside table')
        end = data.find(b'\0', offset)
        if end < 0:
            raise ValueError('unterminated string')
        return data[offset:end].decode('utf-8', errors='replace')

    def blob(self, section):
        return self.data[section['offset']:section['offset'] + section['size']]

    def properties(self):
        """列出GNU AArch64 feature位，首版遇认证/BTI属性保守拒绝。"""
        section = self.by_name.get('.note.gnu.property')
        if not section:
            return []
        raw = self.blob(section)
        features = []
        pos = 0
        while pos < len(raw):
            if pos + 12 > len(raw):
                raise ValueError('truncated GNU note')
            namesz, descsz, typ = struct.unpack_from('<III', raw, pos)
            pos += 12
            name = raw[pos:pos + namesz]
            pos += (namesz + 3) & ~3
            end = pos + descsz
            if end > len(raw):
                raise ValueError('truncated GNU note descriptor')
            if name == b'GNU\0' and typ == 5:
                p = pos
                while p < end:
                    if p + 8 > end:
                        raise ValueError('truncated GNU property')
                    kind, size = struct.unpack_from('<II', raw, p)
                    p += 8
                    if p + size > end:
                        raise ValueError('truncated GNU property payload')
                    if kind == 0xc0000000:
                        if size != 4:
                            raise ValueError('unknown AArch64 feature property width')
                        features.append(struct.unpack_from('<I', raw, p)[0])
                    p += (size + 7) & ~7
            pos = (end + 3) & ~3
        return features

    def code_ranges(self):
        mappings = {}
        for s in self.symbols:
            if s['name'] in ('$x', '$d') or s['name'].startswith(('$x.', '$d.')):
                if 0 < s['shndx'] < len(self.sections):
                    mappings.setdefault(s['shndx'], set()).add((s['value'], s['name'][1]))
        ranges = []
        coverage = Counter()
        for section in self.sections:
            if not section['flags'] & 4 or section['type'] == 8 or section['name'].startswith(('.plt', '.iplt')):
                continue
            points = sorted(mappings.get(section['index'], set()))
            kinds_by_address = {}
            for address, kind in points:
                kinds_by_address.setdefault(address, set()).add(kind)
            if any(len(kinds) != 1 for kinds in kinds_by_address.values()):
                raise ValueError('conflicting code/data mapping symbols')
            if not points:
                coverage['exec_bytes_without_mapping_symbols'] += section['size']
                continue
            for i, (start, kind) in enumerate(points):
                end = points[i + 1][0] if i + 1 < len(points) else section['addr'] + section['size']
                if not section['addr'] <= start <= end <= section['addr'] + section['size']:
                    raise ValueError('mapping symbol outside section')
                if kind == 'x' and start % 4 == end % 4 == 0:
                    ranges.append((start, end, section['offset'] + start - section['addr'], section['name']))
                    coverage['trusted_code_bytes'] += end - start
                else:
                    coverage['data_or_unaligned_bytes'] += end - start
        return ranges, dict(coverage)

    def ordinary_plt(self):
        section = self.by_name.get('.plt')
        rela = self.by_name.get('.rela.plt')
        if not section or not rela:
            return {}, dict(no_plt_or_rela=True)
        if section['size'] < 32 or (section['size'] - 32) % 16 or rela['entsize'] != 24:
            raise ValueError('unsupported PLT layout')
        raw = self.blob(section)
        head = struct.unpack_from('<8I', raw)
        if head[0] != 0xa9bf7bf0 or head[4] != 0xd61f0220 or head[5:] not in ((0xd503201f,) * 3, (0xd4207d00,) * 3):
            raise ValueError('unsupported PLT0 template')
        if head[1] & 0x9f00001f != 0x90000010 or head[2] & 0xffc003ff != 0xf9400211 or head[3] & 0xffc003ff != 0x91000210:
            raise ValueError('unsupported PLT0 address template')
        symbols = self.tables.get(rela['link'])
        if symbols is None:
            raise ValueError('PLT relocation lacks symbol table')
        relocations = {}
        for off in range(rela['offset'], rela['offset'] + rela['size'], 24):
            slot, info, addend = struct.unpack_from('<QQq', self.data, off)
            if info & 0xffffffff == 1026:
                index = info >> 32
                if index >= len(symbols) or slot in relocations or addend:
                    raise ValueError('invalid/duplicate JUMP_SLOT')
                relocations[slot] = symbols[index]
        entries = {}
        ordinary_end = section['size']
        tls_tail_bytes = 0
        # GNU ld会在普通PLT后追加32B TLSDESC resolver，不把它当两条普通entry。
        if len(raw) >= 64:
            tail = struct.unpack_from('<8I', raw, len(raw) - 32)
            if (tail[0] == 0xa9bf0fe2 and tail[1] & 0x9f00001f == 0x90000002 and
                    tail[2] & 0x9f00001f == 0x90000003 and
                    tail[3] & 0xffc003ff == 0xf9400042 and
                    tail[4] & 0xffc003ff == 0x91000063 and tail[5] == 0xd61f0040 and
                    tail[6:] == (0xd503201f, 0xd503201f)):
                ordinary_end -= 32
                tls_tail_bytes = 32
        for offset in range(32, ordinary_end, 16):
            pc = section['addr'] + offset
            a, l, d, b = struct.unpack_from('<4I', raw, offset)
            if a & 0x9f00001f != 0x90000010 or l & 0xffc003ff != 0xf9400211 or d & 0xffc003ff != 0x91000210 or b != 0xd61f0220:
                raise ValueError('unsupported ordinary PLT entry')
            page = (pc & ~4095) + sx(((a >> 5) & 0x7ffff) * 4 + ((a >> 29) & 3), 21) * 4096
            slot = page + ((l >> 10) & 0xfff) * 8
            if slot != page + ((d >> 10) & 0xfff):
                raise ValueError('PLT LDR/ADD address mismatch')
            if slot not in relocations:
                raise ValueError('PLT lacks exact JUMP_SLOT')
            if not any(p['flags'] & 2 and p['vaddr'] <= slot and slot + 8 <= p['vaddr'] + p['memsz'] for p in self.loads):
                raise ValueError('GOT slot outside writable ELF load segment')
            entries[pc] = dict(got_offset=slot, symbol=relocations[slot])
        if len(entries) != len(relocations):
            raise ValueError('PLT/JUMP_SLOT count mismatch')
        return entries, dict(ordinary_entries=len(entries), jump_slots=len(relocations),
                             excluded_tlsdesc_resolver_bytes=tls_tail_bytes)


def load_bias(elf, rows, page_size):
    values = set()
    for m in rows:
        if 'x' not in m['perms']:
            continue
        for p in elf.loads:
            if not p['flags'] & 1:
                continue
            low = p['offset'] // page_size * page_size
            high = (p['offset'] + p['filesz'] + page_size - 1) // page_size * page_size
            if low <= m['offset'] < high:
                values.add(m['start'] - (p['vaddr'] + m['offset'] - p['offset']))
    if len(values) != 1:
        raise ValueError(f'ambiguous ELF load bias: {sorted(values)}')
    return values.pop()


