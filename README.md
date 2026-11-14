# 细粒度聚合结果的安全复用与补偿查询

本项目保存仓库与湖表开发所需的本地资料，提供内容摘要、记录身份与登记关系检查。当前读取入口只返回表数量及身份检查结果，不执行关系计划、版本合并、聚合或查询规划。

资料表包含facts、cache_catalog、requests。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料，data/boundary.json登记特殊业务情形，data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。

运行环境为 Python 3.11及以上，当前仅使用标准库，无需额外服务。包内io.py负责资料读取，domain.py负责表集合与引用检查，cli.py提供本地检查入口。

`reuse_data/planner/` 在此之上实现带覆盖证明的细粒度聚合复用规划器：受限AST登记、SQL三值逻辑谓词包含证明、聚合可导出性（AVG由sum/非空count重建、COUNT DISTINCT身份集合规则）、分区级版本绑定与覆盖切分、成本搜索与预算兜底，以及执行期摘要/来源复验。规划结果由独立逐事实参照引擎做结果袋核对，证明接口与安全约定见 docs/planner.md。

```sh
python -m unittest discover -s tests -v
python -m compileall -q reuse_data tests
python -m reuse_data.cli data/normal.json
python -m reuse_data.cli data/boundary.json
python -m reuse_data.cli plan
```

容器内使用同样的检查入口：

```sh
docker build -t reuse-data .
docker run --rm reuse-data
```
