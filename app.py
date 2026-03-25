"""Streamlit GUI for DEPTH claimable checker.

Dependencies:
  pip install streamlit requests

Run:
  streamlit run app.py
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

import random
import time

import requests
import streamlit as st


# Higher precision for token calculations
getcontext().prec = 50


RPC_DEFAULT = "https://api.mainnet.abs.xyz/"
DEPTHSOUL_ADDR = "0x7C0bab11b67Ac041C2Ff870ef2f7807428aE4CB2"
CLAIM_CONTRACT = "0xd9f34CdA3667E0b5Be4b6fba55D854cFb2eD0694"
GOLDSKY_SUBGRAPH = "https://api.goldsky.com/api/public/project_cmgzljqwl006c5np2gnao4li4/subgraphs/depth-main/1.0.2/gn"

# Function selectors (keccak4) used in the project
SEL_TOKEN_ID_OF = "773c02d4"  # tokenIdOf(address)
SEL_CLAIMABLE = "f9f87c18"     # claimable(uint256)


@dataclass
class Row:
    label: str
    address: str
    token_id: Optional[int]
    claim_raw: Optional[int]

    @property
    def claim_depth(self) -> Optional[Decimal]:
        if self.claim_raw is None:
            return None
        return Decimal(self.claim_raw) / Decimal(10**18)


def normalize_addresses_with_labels(text: str) -> List[tuple[str, str]]:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    pairs: List[tuple[str, str]] = []
    seen = set()
    addr_re = re.compile(r"(0x[0-9a-fA-F]{40})")
    for ln in lines:
        m = addr_re.search(ln)
        if not m:
            continue
        addr = m.group(1)
        # label = text before address (trim), or entire token before if tab/space separated, fallback to empty
        label_part = ln[: m.start()].strip().replace("\t", " ")
        label = label_part if label_part else ""
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        pairs.append((label, addr))
    return pairs


def pad_hex(data_hex: str, length: int = 64) -> str:
    return data_hex.rjust(length, "0")


def build_call(to: str, data: str, call_id: int) -> Dict:
    return {
        "id": call_id,
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": to, "data": data},
            "latest",
        ],
    }


def post_batch(rpc_url: str, payload: List[Dict], retries: int = 3, backoff: float = 0.8) -> List[Dict]:
    """POST a batch with simple retry/backoff to avoid rate limits."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                rpc_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=25,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            sleep_for = backoff * (1 + 0.3 * random.random()) * (2**attempt)
            time.sleep(sleep_for)
    if last_err:
        raise last_err
    return []


def fetch_vault_id_from_subgraph(address: str) -> Optional[int]:
    """Fallback: query Goldsky subgraph to get user's latest vault id.

    Returns the last vault id if present, else None.
    """
    query = {
        "query": """
        query UserVaults($userId: ID!, $first: Int!) {
          user(id: $userId) {
            vaults(first: $first, orderBy: createdAt, orderDirection: desc) {
              id
            }
          }
        }
        """,
        "variables": {"userId": address.lower(), "first": 1},
    }
    try:
        resp = requests.post(
            GOLDSKY_SUBGRAPH,
            headers={"Content-Type": "application/json"},
            data=json.dumps(query),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        vaults = (
            data.get("data", {})
            .get("user", {})
            .get("vaults", [])
        )
        if vaults:
            vid = int(vaults[0]["id"])
            return vid
    except Exception:
        return None
    return None


def decode_uint256(hex_str: str) -> int:
    if not hex_str or not hex_str.startswith("0x"):
        raise ValueError("Invalid hex string")
    return int(hex_str, 16)


def query_token_ids(rpc_url: str, addresses: List[str]) -> Dict[str, Optional[int]]:
    batch = []
    for i, addr in enumerate(addresses, 1):
        data = "0x" + SEL_TOKEN_ID_OF + pad_hex(addr.lower().replace("0x", ""))
        batch.append(build_call(DEPTHSOUL_ADDR, data, i))

    results: Dict[str, Optional[int]] = {a: None for a in addresses}
    if not batch:
        return results

    # split into smaller chunks to reduce RPC pressure
    chunk_size = 10
    resp_by_id: Dict[int, Dict] = {}
    for start in range(0, len(batch), chunk_size):
        chunk = batch[start : start + chunk_size]
        resp = post_batch(rpc_url, chunk)
        for item in resp:
            if isinstance(item, dict) and "id" in item:
                resp_by_id[item["id"]] = item
        # tiny pause between chunks
        time.sleep(0.4)

    for idx, addr in enumerate(addresses, 1):
        item = resp_by_id.get(idx)
        if not item or "result" not in item:
            results[addr] = None
            continue
        try:
            results[addr] = decode_uint256(item.get("result", "0x0"))
        except Exception:
            results[addr] = None
    return results


def query_claimables(rpc_url: str, token_ids: Dict[str, Optional[int]]) -> Dict[str, Optional[int]]:
    batch = []
    items = list(token_ids.items())
    id_map: Dict[int, str] = {}
    for i, (addr, tid) in enumerate(items, 101):
        if tid is None:
            continue
        data = "0x" + SEL_CLAIMABLE + pad_hex(hex(tid).replace("0x", ""))
        batch.append(build_call(CLAIM_CONTRACT, data, i))
        id_map[i] = addr

    results: Dict[str, Optional[int]] = {a: None for a in token_ids}
    if not batch:
        return results

    chunk_size = 10
    resp_by_id: Dict[int, Dict] = {}
    for start in range(0, len(batch), chunk_size):
        chunk = batch[start : start + chunk_size]
        resp = post_batch(rpc_url, chunk)
        for item in resp:
            if isinstance(item, dict) and "id" in item:
                resp_by_id[item["id"]] = item
        time.sleep(0.4)

    for req_id, addr in id_map.items():
        item = resp_by_id.get(req_id)
        if not item or "result" not in item:
            results[addr] = None
            continue
        try:
            results[addr] = decode_uint256(item.get("result", "0x0"))
        except Exception:
            results[addr] = None
    return results


def build_csv(rows: List[Row]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["label", "address", "tokenId", "claimDepth"])
    for r in rows:
        writer.writerow([
            r.label,
            r.address,
            r.token_id if r.token_id is not None else "",
            f"{r.claim_depth:.6f}" if r.claim_depth is not None else "",
        ])
    return buf.getvalue().encode()


def main() -> None:
    st.set_page_config(page_title="DEPTH Claimable Checker", page_icon="🌊", layout="centered")
    st.title("DEPTH Claimable Checker")

    rpc_url = st.text_input("RPC Endpoint", value=RPC_DEFAULT)
    use_subgraph = st.checkbox("子图补全 tokenId（Goldsky）", value=True, help="当直接 RPC 查询不到 tokenId 时，尝试从 Goldsky 子图读取最新 vault id。")
    addr_text = st.text_area(
        "地址列表（可含标签，如 MOD1.LNK 0x...）",
        height=260,
        placeholder="MOD1.LNK\t0x...\nMOD2.LNK 0x...",
    )

    if st.button("查询可领取"):
        pairs = normalize_addresses_with_labels(addr_text)
        if not pairs:
            st.warning("未识别到有效地址")
            return

        total = len(pairs)
        chunk_display = 20
        rows: List[Row] = []
        table_ph = st.empty()
        progress = st.progress(0.0)
        status = st.empty()

        for start in range(0, total, chunk_display):
            end = min(start + chunk_display, total)
            status.write(f"处理中 {start + 1} - {end} / {total} ...")
            sub_pairs = pairs[start:end]
            sub_addresses = [p[1] for p in sub_pairs]

            token_ids = query_token_ids(rpc_url, sub_addresses)

            if use_subgraph:
                for addr in sub_addresses:
                    if token_ids.get(addr) is None:
                        vid = fetch_vault_id_from_subgraph(addr)
                        if vid is not None:
                            token_ids[addr] = vid

            claimables = query_claimables(rpc_url, token_ids)

            for label, addr in sub_pairs:
                rows.append(
                    Row(
                        label=label,
                        address=addr,
                        token_id=token_ids.get(addr),
                        claim_raw=claimables.get(addr),
                    )
                )

            # incremental display
            table_data = [
                {
                    "label": r.label,
                    "address": r.address,
                    "tokenId": r.token_id,
                    "claimDepth": float(r.claim_depth) if r.claim_depth is not None else None,
                }
                for r in rows
            ]
            table_ph.dataframe(table_data, use_container_width=True)
            progress.progress(end / total)
            time.sleep(0.05)

        status.empty()
        st.subheader("结果")
        total_with_tid = sum(1 for r in rows if r.token_id is not None)
        total_with_claim = sum(1 for r in rows if r.claim_depth is not None)
        if total_with_tid == 0:
            st.warning("未获取到任何 tokenId，可能地址未铸造或 RPC 被阻断。")
        elif total_with_claim == 0:
            st.warning("未获取到可领取数据，可能 RPC 返回为空或方法不支持。")

        csv_bytes = build_csv(rows)
        st.download_button(
            "下载 CSV",
            data=csv_bytes,
            file_name="claimable_depth.csv",
            mime="text/csv",
        )

        st.caption("数据单位：DEPTH，精度 18 位小数")


if __name__ == "__main__":
    main()
