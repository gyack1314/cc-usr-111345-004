# 业务资料约定

日期用整数日且from_day包含、to_day不包含。cost为定标整数分，null表示缺失。cache_catalog仅列候选元数据，实际聚合资产须按来源快照和摘要登记，不等于缓存已可直接使用。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，未知版本或内容变化应重新登记。业务身份、金额、版本及测量数值均为本地资料，不代表任何现实机构的经营数据。

- facts：id、tenant、day、cost。
- cache_catalog：id、snapshot、groups、days、states。
- requests：id、snapshot、groups、from_day、to_day、aggregate。

normal与boundary是相互独立的资料包，不能直接拼接重复id。引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，不替代仓库查询语义、快照可见性或递归完备性判断。
