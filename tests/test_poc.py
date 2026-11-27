import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from health_ad_poc.api import APIError, FoodSafetyClient, SearchResult, parse_response
from health_ad_poc.cli import INPUT_FIELDS, demo_results, main, read_products, summarize
from health_ad_poc.config import Settings
from health_ad_poc.matching import match_product


RECORD = {"PRDLST_REPORT_NO": "000000001", "PRDLST_NM": "제품 A 1000", "BSSH_NM": "제조사"}


def payload(rows, total=None):
    return {"I0030": {"RESULT": {"CODE": "INFO-000"}, "row": rows,
                      "total_count": str(len(rows) if total is None else total)}}


def response(value):
    return Mock(status_code=200, content=json.dumps(value, ensure_ascii=False).encode("utf-8"))


class APITests(unittest.TestCase):
    def test_no_data_and_auth_failure_are_distinct(self):
        self.assertEqual(parse_response({"RESULT": {"CODE": "INFO-200"}}), ([], 0))
        with self.assertRaises(APIError) as error:
            parse_response({"RESULT": {"CODE": "INFO-110", "MSG": "secretkey"}})
        self.assertEqual(error.exception.code, "INFO-110")
        self.assertNotIn("secretkey", str(error.exception))

    def test_malformed_response_is_not_no_data(self):
        for bad in ([], {}, payload([RECORD], 0), payload([], 1)):
            with self.subTest(bad=bad), self.assertRaises(APIError):
                parse_response(bad)

    def test_pagination_cache_and_check_bypass(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Mock()
            other = {**RECORD, "PRDLST_REPORT_NO": "000000002"}
            session.get.side_effect = [response(payload([RECORD], 2)), response(payload([other], 2)), response(payload([RECORD], 2))]
            client = FoodSafetyClient(Settings("secretkey", interval=0, page_size=1, cache_dir=Path(temp)), session=session)
            found = client.search(name="제품 A")
            self.assertEqual(len(found.rows), 2)
            self.assertFalse(found.truncated)
            self.assertEqual(client.search(name="제품 A").rows, found.rows)
            self.assertEqual(client.requests_made, 2)
            self.assertEqual(client.cache_hits, 2)
            client.check()
            self.assertEqual(client.requests_made, 3)
            self.assertNotIn("secretkey", "".join(p.read_text() for p in Path(temp).glob("*.json")))

    def test_connection_error_redacts_key(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("https://server/secretkey")
        client = FoodSafetyClient(Settings("secretkey", interval=0), use_cache=False, session=session)
        with self.assertRaises(APIError) as error:
            client.search(name="test")
        self.assertNotIn("secretkey", str(error.exception))

    def test_truncation_and_request_budget(self):
        session = Mock()
        session.get.return_value = response(payload([RECORD], 10))
        client = FoodSafetyClient(Settings("fake", interval=0, page_size=1, max_pages=1, max_requests=1), use_cache=False, session=session)
        self.assertTrue(client.search(name="test").truncated)
        with self.assertRaises(APIError) as error:
            client.search(name="other")
        self.assertEqual(error.exception.code, "REQUEST_BUDGET")

    def test_changed_total_is_rejected(self):
        session = Mock()
        session.get.side_effect = [response(payload([RECORD], 2)), response(payload([RECORD], 3))]
        client = FoodSafetyClient(Settings("fake", interval=0, page_size=1), use_cache=False, session=session)
        with self.assertRaises(APIError) as error:
            client.search(name="test")
        self.assertEqual(error.exception.code, "DATA_CHANGED")


class MatchingTests(unittest.TestCase):
    def match(self, product, rows=None, truncated=False):
        rows = [RECORD] if rows is None else rows
        return match_product(product, SearchResult(rows, len(rows), truncated, []))

    def test_id_or_name_and_manufacturer_exact(self):
        for product in ({"report_no": "000000001", "retail_name": "판매명"},
                        {"search_name": "제품A1000", "manufacturer": "(주)제조사"}):
            self.assertEqual(self.match(product)["status"], "matched")

    def test_name_only_fuzzy_and_wrong_id_need_review(self):
        for product in ({"search_name": "제품 A 1000"},
                        {"search_name": "제품 A 2000", "manufacturer": "제조사"},
                        {"search_name": "제품 A 1000", "manufacturer": "제조사", "report_no": "999"}):
            self.assertEqual(self.match(product)["status"], "needs_review")

    def test_conflict_ambiguity_and_truncation_need_review(self):
        self.assertEqual(self.match({"report_no": "000000001", "manufacturer": "다른제조사"})["reason"], "manufacturer_conflict")
        self.assertEqual(self.match({"report_no": "000000001"}, [RECORD, {**RECORD, "PRDLST_NM": "다른 이름"}])["status"], "needs_review")
        self.assertEqual(self.match({"report_no": "000000001"}, truncated=True)["status"], "needs_review")

    def test_empty_records_are_unmatched(self):
        self.assertEqual(self.match({"search_name": "없음"}, [])["status"], "unmatched")


class WorkflowTests(unittest.TestCase):
    def test_demo_no_network(self):
        with tempfile.TemporaryDirectory() as temp, patch("requests.Session.get", side_effect=AssertionError("network forbidden")):
            output = Path(temp) / "demo"
            self.assertEqual(main(["demo", "--output", str(output)]), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["mode"], "demo_fictional")
            self.assertEqual(summary["requests_made"], 0)
            self.assertEqual(summary["automatic_match_rate"], 0.5)

    def test_api_error_and_not_run_are_separate(self):
        result = summarize(demo_results()[:1] + [{"status": "api_error", "reason": "HTTP_503"},
                                               {"status": "not_run", "reason": "stopped_after_api_error"}])
        self.assertEqual(result["api_error_count"], 1)
        self.assertAlmostEqual(result["automatic_match_rate"], 1 / 3)

    def test_csv_ids_and_required_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "products.csv"
            item = {**dict.fromkeys(INPUT_FIELDS, ""), "sample_id": "S01", "retail_name": "제품",
                    "report_no": "000000001", "source_url": "https://example.com/product"}
            def write(rows):
                with path.open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=INPUT_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
            write([item])
            self.assertEqual(read_products(path)[0]["report_no"], "000000001")
            for rows in ([item, item], [{**item, "retail_name": ""}]):
                write(rows)
                with self.assertRaises(ValueError):
                    read_products(path)


if __name__ == "__main__":
    unittest.main()
