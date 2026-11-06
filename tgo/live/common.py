from pathlib import Path
import hashlib,json,os,re
from tgo.util import sha256 as sha,write_json as atomic_json
ROOT=Path(os.environ.get('TGO_LIVE_WORKSPACE',Path.cwd()))
TOKEN_ENV='TGO_LIVE_SESSION_TOKEN'
def canonical_function(name):return re.sub(r'/[0-9]+$','',name).replace('.localalias','')
def profile_function(name):
 m=re.fullmatch(r'__BOLT_FDE_FUNCat([0-9a-fA-F]+)(?:/[0-9]+)?',name)
 return '@'+format(int(m[1],16),'x') if m else canonical_function(name)
def profile_score(scores,name,vaddr):return max(scores.get(canonical_function(name),0),scores.get('@'+format(vaddr,'x'),0))
