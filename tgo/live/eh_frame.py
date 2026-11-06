#!/usr/bin/env python3
"""重定位标准AArch64 .eh_frame，合并补充FDE后只登记一个代码对象。"""
import struct

def leb(data,p,signed=False):
    value=0;shift=0
    while True:
        byte=data[p];p+=1;value|=(byte&127)<<shift;shift+=7
        if not byte&128:break
        if shift>63:raise ValueError('LEB越界')
    if signed and byte&64:value-=1<<shift
    return value,p

def relocate(blob,source_address,target_address,keep_ranges=None):
    raw=bytearray(blob);cies={};ranges=[];pos=0;fields=[];ignored=0
    def pointer(p,encoding,adjust=True):
        if encoding==255:return 0,p
        fmt=encoding&15;relative=encoding&0x70
        sizes={0:8,2:2,3:4,4:8,10:2,11:4,12:8}
        if fmt not in sizes or relative not in (0,0x10):raise ValueError('不支持的EH指针编码:'+hex(encoding))
        size=sizes[fmt];signed=fmt in (10,11,12)
        value=int.from_bytes(raw[p:p+size],'little',signed=signed)
        actual=source_address+p+value if relative==0x10 and value else value
        if adjust and value:
            if relative==0x10:newvalue=value+source_address-target_address
            else:raise ValueError('新CFI有未重定位绝对指针')
            try:raw[p:p+size]=newvalue.to_bytes(size,'little',signed=signed)
            except OverflowError:raise ValueError('重定位EH指针超距')
            fields.append({'offset':p,'encoding':encoding,'original':value,'rebased':newvalue})
        return actual,p+size
    while pos+4<=len(raw):
        length=struct.unpack_from('<I',raw,pos)[0]
        if length==0:
            if any(raw[pos:]):raise ValueError('EH终止项后有非零数据')
            return bytes(raw[:pos]+bytes(4)),{'cies':len(cies),'fdes':len(ranges),'ranges':ranges,'relocated_fields':fields,'ignored_original_fdes':ignored}
        if length==0xffffffff or length<4 or pos+4+length>len(raw):raise ValueError('CFI记录长度不支持')
        end=pos+4+length;identifier=struct.unpack_from('<I',raw,pos+4)[0];p=pos+8
        if identifier==0:
            version=raw[p];p+=1
            if version not in (1,3):raise ValueError('CIE版本不支持')
            z=raw.index(0,p,end);augmentation=bytes(raw[p:z]).decode();p=z+1
            _,p=leb(raw,p);_,p=leb(raw,p,True)
            if version==1:p+=1
            else:_,p=leb(raw,p)
            info={'encoding':0,'lsda':255,'z':augmentation.startswith('z')}
            if augmentation:
                if not info['z']:raise ValueError('只支持z增强CIE')
                size,p=leb(raw,p);augend=p+size
                for letter in augmentation[1:]:
                    if letter=='R':info['encoding']=raw[p];p+=1
                    elif letter=='L':info['lsda']=raw[p];p+=1
                    elif letter=='P':encoding=raw[p];p+=1;_,p=pointer(p,encoding)
                    elif letter=='S':pass
                    else:raise ValueError('未知CIE增强:'+letter)
                if p!=augend:raise ValueError('CIE增强长度不符')
            cies[pos]=info
        else:
            ciepos=pos+4-identifier
            if ciepos not in cies:raise ValueError('FDE没有前置CIE')
            info=cies[ciepos]
            start,p=pointer(p,info['encoding'])
            size,p=pointer(p,info['encoding']&15,False)
            if info['z']:
                auglen,p=leb(raw,p);augend=p+auglen
                if info['lsda']!=255:_,p=pointer(p,info['lsda'])
                if p!=augend:raise ValueError('FDE增强长度不符')
            keep=keep_ranges is None or any(a<=start and start+size<=b for a,b in keep_ranges)
            if keep:ranges.append({'start':start,'size':size})
            else:
                # 原ELF继续提供旧函数CFI。保留记录偏移，用无引用CIE替换旧FDE，
                # 避免一个动态对象的PC范围跨过其它库和近端跳板。
                dummy=bytes([1,0,1,0x78,30])
                if end-(pos+8)<len(dummy):raise ValueError('旧FDE过短，无法保持偏移过滤')
                raw[pos+4:pos+8]=bytes(4)
                raw[pos+8:end]=dummy+bytes(end-pos-8-len(dummy));ignored+=1
        if p>end:raise ValueError('CFI字段超出记录')
        pos=end
    raise ValueError('CFI缺少终止项')
