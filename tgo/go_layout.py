"""Profile-guided package-local layout; Go's linker still emits all metadata."""
from collections import Counter, defaultdict
from pathlib import Path
import json


def profile_order(preprocessed):
    lines = Path(preprocessed).read_text().splitlines()
    if not lines or lines[0] != "GO PREPROFILE V1" or (len(lines) - 1) % 3:
        raise ValueError("unsupported Go preprofile format for function layout")
    edges, heat = defaultdict(int), defaultdict(int)
    for i in range(1, len(lines), 3):
        caller, callee = lines[i:i+2]
        _, weight = map(int, lines[i+2].split())
        if weight <= 0:
            continue
        heat[caller] += weight
        heat[callee] += weight
        if caller != callee:
            edges[tuple(sorted((caller, callee)))] += weight
    # Greedy weighted chains keep frequently observed neighbours together.
    # Package boundaries are enforced by the linker, never by guessing how a
    # dot inside a Go package path should be parsed.
    owner = {name: name for name in heat}
    chains = {name: [name] for name in heat}
    for (a, b), weight in sorted(edges.items(), key=lambda item: (-item[1], item[0])):
        x, y = owner[a], owner[b]
        if x == y:
            continue
        left, right = chains[x], chains[y]
        if a not in (left[0], left[-1]) or b not in (right[0], right[-1]) or len(left)+len(right) > 64:
            continue
        merged = (left if a == left[-1] else left[::-1]) + (right if b == right[0] else right[::-1])
        chains[x] = merged
        del chains[y]
        for name in merged:
            owner[name] = x
    ordered = sorted(chains.values(), key=lambda chain: (-sum(heat[n] for n in chain), tuple(chain)))
    return [name for chain in ordered for name in chain], dict(heat)


# Injected before compilation-unit Textp lists are constructed. The original
# package dependency order, DupTextSyms, runtime and FIPS ranges are preserved.
LINKER_HOOK = r'''
func (l *Loader) tgoOrderPackageText(libs []*sym.Library) {
    orderPath := os.Getenv("TGO_GO_LAYOUT_ORDER")
    if orderPath == "" { return }
    data, err := os.ReadFile(orderPath)
    if err != nil { panic(fmt.Sprintf("tgo layout order: %v", err)) }
    ranks := make(map[string]int)
    for i, name := range strings.Split(string(data), "\n") {
        if name != "" { ranks[name] = i }
    }
    report, err := os.Create(os.Getenv("TGO_GO_LAYOUT_REPORT"))
    if err != nil { panic(fmt.Sprintf("tgo layout report: %v", err)) }
    defer report.Close()
    var rows strings.Builder
    rows.WriteString("package\tbefore\tafter\tname\tsize\n")
    for _, lib := range libs {
        if lib.Pkg == "runtime" || strings.HasPrefix(lib.Pkg, "internal/runtime/") ||
            strings.HasPrefix(lib.Pkg, "crypto/internal/fips140") { continue }
        ordinary := true
        for _, s := range lib.Textp {
            if l.SymType(s) != sym.STEXT { ordinary = false; break }
        }
        if !ordinary { continue }
        original := make(map[Sym]int, len(lib.Textp))
        for i, s := range lib.Textp { original[s] = i }
        var slots []int
        var ordered []Sym
        for i, s := range lib.Textp {
            if strings.HasPrefix(l.SymName(s), lib.Pkg+".") {
                slots = append(slots, i)
                ordered = append(ordered, s)
            }
        }
        sort.SliceStable(ordered, func(i, j int) bool {
            a, okA := ranks[l.SymName(ordered[i])]
            b, okB := ranks[l.SymName(ordered[j])]
            return okA && (!okB || a < b)
        })
        for j, s := range ordered {
            i := slots[j]
            lib.Textp[i] = s
            fmt.Fprintf(&rows, "%s\t%d\t%d\t%s\t%d\n", lib.Pkg, original[s], i, l.SymName(s), l.SymSize(s))
        }
    }
    if _, err := report.WriteString(rows.String()); err != nil { panic(err) }
}
'''


def linker_overlay(goroot, root):
    source = Path(goroot) / "src/cmd/link/internal/loader/loader.go"
    text = source.read_text()
    anchor = "\t// Now assemble global textp, and assign text symbols to units."
    if text.count(anchor) != 1 or "func (l *Loader) AssignTextSymbolOrder(" not in text:
        raise ValueError("Go linker source is incompatible with package-local layout overlay")
    root = Path(root).resolve()
    root.mkdir()
    patched = root / "loader.go"
    patched.write_text(text.replace(anchor, "\tl.tgoOrderPackageText(libs)\n\n" + anchor) + LINKER_HOOK)
    overlay = root / "overlay.json"
    overlay.write_text(json.dumps({"Replace": {str(source): str(patched)}}))
    return source, overlay


def layout_report(path, heat, nm_text):
    rows = []
    for line in Path(path).read_text().splitlines()[1:]:
        package, before, after, name, size = line.split("\t")
        rows.append({"package": package, "before": int(before), "after": int(after),
                     "name": name, "size": int(size)})
    addresses, occurrences = {}, Counter()
    for line in nm_text.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[2] in ("T", "t"):
            addresses[fields[3]] = int(fields[0], 16)
            occurrences[fields[3]] += 1
    # Go permits distinct local/ABI symbols with the same printed name. nm's
    # name-only output cannot identify these; never guess which PC it means.
    row_counts = Counter(r["name"] for r in rows)
    ambiguous = {name for name in heat if row_counts[name] > 1 or occurrences[name] > 1}
    matched = [r for r in rows if r["name"] in heat and r["name"] in addresses and r["name"] not in ambiguous]
    packages = defaultdict(list)
    for row in matched:
        packages[row["package"]].append(row)
    inversions = sum(addresses[a["name"]] >= addresses[b["name"]]
                     for group in packages.values() for a, b in zip(group, group[1:]))
    result = {"eligible_symbols": len(rows), "profile_symbols": len(heat), "matched_symbols": len(matched),
        "moved_symbols": sum(r["before"] != r["after"] for r in rows),
        "ambiguous_profile_names": sorted(ambiguous),
        "matched_edge_endpoint_weight": sum(heat[r["name"]] for r in matched),
        "total_edge_endpoint_weight": sum(heat.values()), "final_address_order_inversions": inversions,
        "packages": {k: len(v) for k, v in packages.items()}}
    if not result["moved_symbols"] or not matched or inversions:
        raise ValueError(f"Go function layout did not reach the final ELF as requested: {result}")
    return result
