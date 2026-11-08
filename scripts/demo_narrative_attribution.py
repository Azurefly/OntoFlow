"""Synthetic offline role/coreference demo, no DB, LLM or OpenMind required.

Usage: python -m scripts.demo_narrative_attribution
"""
import json
from app.ontology_abstraction import discover_narrative_types

EXAMPLES = {
    'clear_reporter': '李某到达派出所。随后其报警。',
    'ambiguous_reporter': '张某与李某发生争执。随后其报警。',
    'negative': '李某没有向派出所报警。张某否认向李某转账500元。',
    'roles_and_relations': '李某向派出所报案，随后李某驾驶电动车离开。',
    'reported_transfer': '王某称张某向李某转账500元。',
}


def main():
    for label, source in EXAMPLES.items():
        output = discover_narrative_types(source)
        print(json.dumps({
            'scenario': label,
            'schema_entities': [x['name'] for x in output['entities']],
            'schema_attributes': [{'owner': x['owner'], 'name': x['name']}
                                  for x in output['attributes']],
            'schema_relations': [{'source': x['source'], 'predicate': x['predicate'],
                                  'target': x['target']} for x in output['relations']],
            'clarifications': [x['reason'] for x in output['_unresolved']],
        }, ensure_ascii=False))


if __name__ == '__main__':
    main()
