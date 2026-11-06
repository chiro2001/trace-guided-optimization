"""按当前ELF结构生成策略，不维护应用文件SHA或特定版本白名单。"""
from pathlib import Path
from .capabilities import classify_profile_image


def resolve_recipe(args, state, profile):
    requested=getattr(args,'recipe','auto')
    if requested=='standard':return None
    observations=[classify_profile_image(row) for row in profile['images']]
    return {'name':'structural-v1','requested':requested,'known_input_allowlist':False,
        'images':[str(row['path']) for row in profile['images']],
        'observations':observations,'selection':'ROI热度排序与显式--top/--image约束',
        'scope':'当前ELF、运行时结构与生成工具能力；哈希仅绑定采集与事务输入',
        'compatibility_alias':requested=='eager-v12',
        'python_source_rebuilt':bool(state.get('training_images'))}


def recovered_policy(args, row):
    """保留旧调用接口；未经独立证明不自动启用整页代码假设。"""
    plan=getattr(args,'_recipe_plan',None)
    if not plan:return False
    observed=next((r for r in plan['observations'] if r['path']==str(Path(row['path']).resolve())),None)
    return bool(observed and observed['recovery']['assume_code_pages_safe'])
