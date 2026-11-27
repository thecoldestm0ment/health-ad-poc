import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .api import APIError, DOC_URL, FoodSafetyClient, SearchResult
from .config import ConfigError, ROOT, Settings
from .matching import match_product

INPUT_FIELDS = ["sample_id", "category", "retail_name", "search_name", "report_no",
                "manufacturer", "source_url", "collected_at", "notes"]


def read_products(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not set(INPUT_FIELDS) <= set(reader.fieldnames):
            raise ValueError("CSV 헤더가 올바르지 않습니다. data/products.template.csv를 사용하세요.")
        products = []
        seen = set()
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV {number}행: 열 개수가 맞지 않습니다.")
            row = {key: value.strip() for key, value in row.items()}
            if not any(row.values()):
                continue
            if not row["sample_id"] or row["sample_id"] in seen:
                raise ValueError(f"CSV {number}행: sample_id가 없거나 중복입니다.")
            if not row["retail_name"] or not row["source_url"].startswith(("https://", "http://")):
                raise ValueError(f"CSV {number}행: 실제 판매 제품명 retail_name과 source_url을 입력하세요.")
            if row["report_no"] and not (row["report_no"].isascii() and row["report_no"].isdigit()):
                raise ValueError(f"CSV {number}행: report_no는 숫자 문자열로 입력하세요.")
            seen.add(row["sample_id"])
            products.append(row)
    if not products:
        raise ValueError("입력된 제품이 없습니다.")
    return products


def summarize(results):
    count = len(results)
    counts = Counter(row["status"] for row in results)
    matched = [r for r in results if r["status"] == "matched"]
    function_count = sum(bool(str(r["selected"].get("PRIMARY_FNCLTY") or "").strip()) for r in matched)
    return {
        "total_products": count, "status_counts": dict(counts),
        "automatic_match_count": len(matched),
        "automatic_match_rate": len(matched) / count if count else 0,
        "functionality_available_count": function_count,
        "functionality_coverage_all_products": function_count / count if count else 0,
        "api_error_count": counts["api_error"],
        "reason_counts": dict(Counter(r["reason"] for r in results)),
        "note": "자동 매칭 후보 비율이며 정확도가 아닙니다. 후보의 품목제조번호를 제품 표시사항과 수동 대조하세요. "
                "API 오류는 미매칭과 별도로 집계합니다. 검색 실패는 불법·허위광고의 근거가 아닙니다.",
    }


def save_run(results, output, *, mode, requests_made=0, cache_hits=0):
    output.mkdir(parents=True, exist_ok=False)
    summary = {"mode": mode, "created_at": datetime.now(timezone.utc).isoformat(),
               "source": DOC_URL, "requests_made": requests_made, "cache_hits": cache_hits,
               **summarize(results)}
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["sample_id", "category", "retail_name", "status", "reason", "candidate_count",
              "official_report_no", "official_name", "official_manufacturer", "primary_functionality",
              "source_url", "manual_verdict", "manual_notes"]
    with (output / "matches.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for result in results:
            product, selected = result["product"], result["selected"] or {}
            row = {"sample_id": product["sample_id"], "category": product.get("category", ""),
                   "retail_name": product["retail_name"], "status": result["status"], "reason": result["reason"],
                   "candidate_count": len(result["candidates"]),
                   "official_report_no": selected.get("PRDLST_REPORT_NO", ""),
                   "official_name": selected.get("PRDLST_NM", ""),
                   "official_manufacturer": selected.get("BSSH_NM", ""),
                   "primary_functionality": selected.get("PRIMARY_FNCLTY", ""),
                   "source_url": product.get("source_url", ""), "manual_verdict": "", "manual_notes": ""}
            # Keep spreadsheet exports from evaluating product names as formulas.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                             for k, v in row.items()})
    return summary


def demo_results():
    # Entirely fictional fixtures: these are never claimed as market/API observations.
    record = {"PRDLST_REPORT_NO": "00000000000001", "PRDLST_NM": "데모 비타민C",
              "BSSH_NM": "데모제조사", "PRIMARY_FNCLTY": "[데모용 가상 문구] 기능성 데이터 제공 예시"}
    cases = [
        ({"report_no": "00000000000001", "retail_name": "데모 비타민C 60정"}, [record]),
        ({"search_name": "데모 비타민C", "manufacturer": "데모제조사"}, [record]),
        ({"search_name": "데모 비타민C"}, [record]),
        ({"search_name": "없는 데모제품"}, []),
    ]
    results = []
    for i, (fields, records) in enumerate(cases, 1):
        product = {**dict.fromkeys(INPUT_FIELDS, ""), "sample_id": f"DEMO{i:02d}",
                   "category": "가상 데이터", "retail_name": "데모 비타민C", **fields}
        results.append(match_product(product, SearchResult(records, len(records), False, [])))
    return results


def parser():
    result = argparse.ArgumentParser(description="실험 1: 식품안전나라 I0030 제품 매칭 PoC")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="인증키·I0030 권한·실제 API 연결 확인 (1회 요청)")
    lookup = commands.add_parser("lookup", help="제품명 또는 품목제조번호로 공식 기록 조회")
    group = lookup.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="표시사항에 적힌 제품명")
    group.add_argument("--report-no", help="품목제조번호")
    lookup.add_argument("--manufacturer", default="", help="제조업소명 (브랜드/판매원과 다름)")
    lookup.add_argument("--refresh", action="store_true", help="캐시 대신 API 재조회")
    run = commands.add_parser("run", help="입력 CSV의 모든 제품 조회·매칭·결과 저장")
    run.add_argument("--input", type=Path, default=ROOT / "data" / "products.csv")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--output", type=Path)
    validate = commands.add_parser("validate", help="키 없이 입력 CSV 오류 확인")
    validate.add_argument("--input", type=Path, default=ROOT / "data" / "products.csv")
    demo = commands.add_parser("demo", help="키·네트워크 없이 가상 데이터로 전체 흐름 실행")
    demo.add_argument("--output", type=Path)
    return result


def output_path(explicit, mode):
    return explicit or ROOT / "outputs" / f"{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            path = output_path(args.output, "demo")
            summary = save_run(demo_results(), path, mode="demo_fictional")
            print("가상 데이터 데모 완료. 실제 API/판매 제품의 실험 결과가 아닙니다.")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print(f"결과: {path}")
            return 0
        products = None
        if args.command in {"run", "validate"}:
            products = read_products(args.input)
            print(f"입력 검증 완료: {len(products)}개, 카테고리 {dict(Counter(p['category'] for p in products))}")
            if args.command == "validate":
                return 0
        client = FoodSafetyClient(Settings.from_env(), use_cache=not getattr(args, "refresh", False))
        if args.command == "check":
            found = client.check()
            print(f"API 연결 및 I0030 조회 성공. 공식 DB 전체 건수: {found.total_count}")
            return 0
        if args.command == "lookup":
            found = client.search(name=args.name or "", report_no=args.report_no or "", manufacturer=args.manufacturer)
            path = output_path(None, "lookup")
            path.mkdir(parents=True)
            (path / "records.json").write_text(json.dumps({"total_count": found.total_count,
                "truncated": found.truncated, "pages": found.pages}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"total_count": found.total_count, "truncated": found.truncated,
                              "preview": found.rows[:5]}, ensure_ascii=False, indent=2))
            print(f"전체 조회 기록: {path / 'records.json'}")
            return 0
        output = output_path(args.output, "run")
        if output.exists():
            raise ValueError("출력 폴더가 이미 있습니다. 새 --output 경로를 사용하세요.")
        results = []
        for index, product in enumerate(products, 1):
            try:
                # A supplied identifier never silently falls back to an unrelated name.
                found = client.search(report_no=product["report_no"]) if product["report_no"] else client.search(
                    name=product["search_name"] or product["retail_name"])
                row = match_product(product, found)
                row["evidence_pages"] = found.pages
            except APIError as exc:
                row = {"product": product, "status": "api_error", "reason": exc.code,
                       "selected": None, "candidates": [], "error": str(exc)}
            results.append(row)
            print(f"[{index}/{len(products)}] {product['sample_id']}: {row['status']} ({row['reason']})")
            if row["status"] == "api_error":
                # Stop on infrastructure failures rather than spending the quota 20 times.
                for pending in products[index:]:
                    results.append({"product": pending, "status": "not_run", "reason": "stopped_after_api_error",
                                    "selected": None, "candidates": []})
                break
        summary = save_run(results, output, mode="live", requests_made=client.requests_made, cache_hits=client.cache_hits)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"結果 / 결과: {output}")
        return 1 if summary["api_error_count"] else 0
    except (ConfigError, APIError, ValueError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단했습니다. 완료된 API 응답은 캐시에 보관됩니다.", file=sys.stderr)
        return 130
