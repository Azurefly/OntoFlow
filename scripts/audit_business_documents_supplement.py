"""Supplementary business sources selected after initial download failures.

This is an additional named batch, not silent replacement of failed downloads.
Application code, audit mechanics and original first-batch results stay frozen.
"""
from scripts import audit_business_documents as audit

audit.SAMPLES = [
    {
        'id': 'cppcc_cloud_service',
        'file': 'cppcc_cloud_service.docx',
        'title': '北京市政协2026年信息系统政务云基础服务采购项目采购需求',
        'domain': '政务云资源、运行服务、服务指标与采购验收',
        'landing': 'https://ggzyfw.beijing.gov.cn/xmcgfzbgg/20260107/5397747.html',
        'url': 'https://ggzyfw.beijing.gov.cn/cmsbj/u/cms/cn.gov.bjggzyfw.www/202601/07163512zpri.docx',
        'anchors': ['政务云', '存储', '安全', '验收', '备份', '服务'],
    },
    {
        'id': 'urban_network_maintenance',
        'file': 'urban_network_maintenance.pdf',
        'title': '北京市城市管理委员会网络运行维护竞争性磋商文件',
        'domain': '网络运行维护、业务服务约束、供应商评审、合同与履约验收',
        'landing': 'https://cgj.beijing.gov.cn/xxgk/zfcg/zbgg1/202503/P020250529522783359344.pdf',
        'url': 'https://cgj.beijing.gov.cn/xxgk/zfcg/zbgg1/202503/P020250529522783359344.pdf',
        'anchors': ['采购需求', '供应商', '网络', '合同', '验收', '评审'],
    },
    {
        'id': 'hospital_outpatient_service',
        'file': 'hospital_outpatient_service.pdf',
        'title': '信阳市中心医院门诊导诊外包项目（二次）招标文件',
        'domain': '医院门诊导诊、外包人员职责、服务要求、绩效与验收',
        'landing': 'https://xinyang.zfcg.henan.gov.cn/cmsweb81e27e/xinyang/rootfiles/2025/07/07/245012e0fedf4879a83103c49a7579ef.pdf',
        'url': 'https://xinyang.zfcg.henan.gov.cn/cmsweb81e27e/xinyang/rootfiles/2025/07/07/245012e0fedf4879a83103c49a7579ef.pdf',
        'anchors': ['门诊', '导诊', '培训', '岗位', '考核', '合同'],
    },
]

if __name__ == '__main__':
    audit.main()
