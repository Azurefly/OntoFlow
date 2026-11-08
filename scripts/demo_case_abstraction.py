"""Offline smoke demonstration: only class/property/relation schema, no instances.

Run: python -m scripts.demo_case_abstraction
No model, database, OpenMind or credentials are required.
"""
import json
from app import brain

CASE = ('2026年11月2日20时许，张某在某商场门口与李某发生争执。'
        '张某向李某转账500元，随后李某驾驶电动车离开现场。')


def main():
    segment={'content':CASE,'kind':'prose','section':'案件叙述','locator':'示例行 1'}
    with brain.using_model({'base_url':'','model':'','api_key':''}):
        result=brain.joint_extract_segment(segment)
    print(json.dumps({
        'entities':[v['name'] for v in result['entities']],
        'attributes':[{'owner':v['owner'],'name':v['name'],'datatype':v['datatype']}
                      for v in result['attributes']],
        'relations':[{'source':v['source'],'predicate':v['predicate'],'target':v['target']}
                     for v in result['relations']],
        'needs_clarification':result['_unresolved'],
    },ensure_ascii=False,indent=2))


if __name__=='__main__':main()
