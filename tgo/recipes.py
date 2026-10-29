"""Versioned, content-bound optimization policies, separate from backend choice."""
from pathlib import Path
from .util import IdentityError

EAGER_V12 = {
 'libpython3.12.so.1.0': 'de439dc24ea47b35d5e50ee269c7421b5f0eb5d4d129a9ab7ef1210d487c3c88',
 'libtorch_python.so': '6b2c97897785c9d4d89af21cf587f346ba532ac03bc875ff63a72421a09f6577',
 '_multiarray_umath.cpython-312-aarch64-linux-gnu.so': 'fb1837bd38404656b3030d6945c4bceb253d6b308a0e45058af58de4b0725cf0',
 'libc10.so': '6af47532fcb08495ac2094cf3dec3cb71c07fee89b00061adb037ce6b4ba17d1',
 'libophost_transformer.so': '3f5758ed76ce537f3d005bb03b55e3030b210e206f5cb9809ab4346b98bae33b',
 'libtorch_cpu.so': 'd9529119ac18ba154521854dc8da477c25c9a34c3265c95e40086a593d741926',
 'libtorch_npu.so': '83fb9a0eb249aef6bca7f8463047fcb4d3062cc4849f17ddf31a5e050c838842',
 'libnnopbase.so': '308dda461704a6e5f70de6e0eac4495e9829b9cec3328563b75a5b382c6e438b',
}


def resolve_recipe(args, state, profile):
    requested = getattr(args, 'recipe', 'standard')
    if requested == 'standard': return None
    found = {Path(row['path']).name:row for row in profile['images']}
    # A private compiler-produced replacement can change Python's identity;
    # its input and actual mapped output must both match the training manifest.
    replacements = {row['optimized']:row for row in state.get('training_images', [])}
    mismatch = []
    for name, digest in EAGER_V12.items():
        row = found.get(name)
        replacement = replacements.get(row['path']) if row else None
        valid = row and (row['sha256'] == digest or (
            name == 'libpython3.12.so.1.0' and replacement and
            replacement['input']['sha256'] == digest and
            replacement['output']['sha256'] == row['sha256']))
        if not valid: mismatch.append(name)
    if mismatch:
        if requested == 'eager-v12':
            raise IdentityError('eager-v12 recipe requires the complete verified ELF set; mismatch: '+', '.join(mismatch))
        return None
    return {'name':'eager-v12','images':list(EAGER_V12),
            'scope':'content-bound eight-DSO eager host recipe',
            'python_source_rebuilt':bool(replacements)}


def recovered_policy(args, row):
    if not getattr(args, '_recipe_plan', None): return False
    name = Path(row['path']).name
    return name != 'libpython3.12.so.1.0' and EAGER_V12.get(name) == row['sha256']
