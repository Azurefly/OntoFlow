from copy import deepcopy
import pytest
from scripts.semantic_preview import validate_for_review

def snapshot():
    return {'entities':[{'id':'equipment','name':'设备','kind':'class','code':'equipment'},
                        {'id':'event','name':'事件','kind':'class','code':'event'}],
            'attributes':[], 'relations':[]}

def test_attribute_may_not_reuse_entity_code():
    state=snapshot();before=deepcopy(state)
    with pytest.raises(ValueError,match='CODE'):
        validate_for_review('attribute',{'owner_id':'equipment','name':'状态',
             'code':'equipment','datatype':'string'},[],state)
    assert state==before

def test_relation_may_not_reuse_entity_code():
    with pytest.raises(ValueError,match='CODE'):
        validate_for_review('relation',{'source_id':'equipment','target_id':'event',
             'name':'参与事件','code':'event','predicate':'participates_in'},[],snapshot())

def test_valid_code_and_name_accepted_without_mutation():
    state=snapshot();before=deepcopy(state)
    item=validate_for_review('attribute',{'owner_id':'equipment','name':'状态',
         'code':'equipment_status','datatype':'string'},[],state)
    assert item['code']=='equipment_status'
    assert state==before

def test_ascii_only_display_label_not_accepted_as_chinese_name():
    with pytest.raises(ValueError,match='中文'):
        validate_for_review('attribute',{'owner_id':'equipment','name':'status',
             'code':'equipment_status','datatype':'string'},[],snapshot())
