"""本地入口：资料完整性检查（默认）与聚合复用规划演示（plan 子命令）。"""
import argparse
import json

from .io import load, DataError
from .domain import inspect_tables


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def run_check(manifest: str) -> None:
    _print(inspect_tables(load(manifest)))


def run_plan() -> None:
    # 规划器使用自带的受控小资料夹具，不读取外部不可信数据
    from .planner.scenarios import all_scenarios
    report = []
    for r in all_scenarios():
        report.append({
            'scenario': r.name,
            'description': r.description,
            'reference_match': r.match,
            'reused': r.plan['reused'],
            'scan_stats': r.stats,
            'cache_blocks': [
                {'cache_id': b['cache_id'],
                 'partitions': b['partitions'],
                 'residual_predicate': b['residual_predicate']}
                for b in r.plan['cache_blocks']
            ],
            'base_scans': [
                {'partitions': s['partitions'], 'predicate': s['predicate']}
                for s in r.plan['base_scans']
            ],
            'exclusions': r.plan['exclusions'],
            'result_rows': r.result_rows,
            'reference_rows': r.reference_rows,
            'extra': r.extra,
        })
    _print(report)


def main():
    parser = argparse.ArgumentParser(
        description='资料检查与聚合复用规划',
        usage='%(prog)s [manifest] | %(prog)s plan')
    parser.add_argument('rest', nargs='*', help='资料清单，或子命令 plan')
    args = parser.parse_args()
    try:
        if args.rest and args.rest[0] == 'plan':
            run_plan()
        else:
            manifest = args.rest[0] if args.rest else 'data/normal.json'
            run_check(manifest)
    except (DataError, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
