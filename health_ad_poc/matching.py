"""Conservative deterministic matching; similarity only ranks review candidates."""

from difflib import SequenceMatcher
import re
import unicodedata

from .api import SearchResult


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(c for c in text if c.isalnum())


def company(value):
    return normalize(re.sub(r"주식회사|\(주\)|㈜", "", value or ""))


def match_product(product, search: SearchResult):
    target_name = normalize(product.get("search_name") or product.get("retail_name"))
    target_no = product.get("report_no", "").strip()
    target_company = company(product.get("manufacturer", ""))
    candidates = []
    for row in search.rows:
        name_equal = bool(target_name) and target_name == normalize(row.get("PRDLST_NM"))
        id_equal = bool(target_no) and target_no == str(row.get("PRDLST_REPORT_NO", "")).strip()
        company_equal = bool(target_company) and target_company == company(row.get("BSSH_NM", ""))
        company_conflict = bool(target_company) and not company_equal
        score = round(SequenceMatcher(None, target_name, normalize(row.get("PRDLST_NM"))).ratio(), 4)
        candidates.append({"record": row, "name_exact": name_equal, "id_exact": id_equal,
                           "manufacturer_exact": company_equal, "manufacturer_conflict": company_conflict,
                           "name_similarity": score})
    candidates.sort(key=lambda x: (x["id_exact"], x["name_exact"], x["manufacturer_exact"], x["name_similarity"]), reverse=True)
    status, reason, selected = "unmatched", "no_records", None
    exact = [c for c in candidates if c["id_exact"]] if target_no else [
        c for c in candidates if c["name_exact"] and c["manufacturer_exact"]]
    if search.truncated:
        status, reason = "needs_review", "search_truncated"
    elif len(exact) == 1 and not exact[0]["manufacturer_conflict"] and exact[0]["record"].get("PRDLST_REPORT_NO"):
        status, reason, selected = "matched", "report_no_exact" if target_no else "name_and_manufacturer_exact", exact[0]["record"]
    elif len(exact) > 1:
        status, reason = "needs_review", "multiple_exact_records"
    elif candidates:
        status, reason = "needs_review", "identifier_not_found" if target_no else "name_only_or_similar"
        if len(exact) == 1 and exact[0]["manufacturer_conflict"]:
            reason = "manufacturer_conflict"
    return {"product": product, "status": status, "reason": reason, "selected": selected,
            "total_count": search.total_count, "truncated": search.truncated, "candidates": candidates}
