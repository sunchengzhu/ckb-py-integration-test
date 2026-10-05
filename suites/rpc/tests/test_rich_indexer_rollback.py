"""Real devnet regression tests for PR 5300, from the confirmed rollback design.

All state is local: the repository's Dummy-PoW dev spec, an always-success
genesis system cell, two owned node processes and one unique SQL database.
The runtime manifest is prepared separately from the tests at the pinned head.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
from functools import lru_cache

import pytest
import requests
import toml

from framework.config import CKB_DEFAULT_CONFIG
from framework.rpc import RPCClient
from framework.util import create_config_file


PROJECT = Path(__file__).resolve().parents[3]
FIXTURES = PROJECT / "suites/rpc/fixtures"
PRODUCT_HEAD = "33e4eec7502b754ddf61282c04e0a76a29e27b26"
BACKENDS = ["postgres", "sqlite"]
COMMON_HEIGHT = 5
FEE = 10_000
CKB = 100_000_000
WAIT_SECONDS = 45
PAGE_SIZE = 2


@lru_cache(maxsize=1)
def _runtime():
    manifest = PROJECT / "suites/rpc/.runtime/ckb5300-runtime.json"
    assert manifest.is_file(), f"Prepared PR-5300 runtime manifest unavailable: {manifest}"
    runtime = json.loads(manifest.read_text())
    assert runtime["source_head"] == PRODUCT_HEAD, "Runtime does not identify the selected product head"
    binary = Path(runtime["ckb_binary"])
    assert binary.is_file() and os.access(binary, os.X_OK), f"CKB binary unavailable: {binary}"
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert digest == runtime["ckb_binary_sha256"], "Prepared CKB binary changed after runtime attestation"
    return runtime


def _port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _wait(description, predicate, nodes, timeout=WAIT_SECONDS, startup=False):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        for node in nodes:
            node.assert_running()
        try:
            last = predicate()
            if last:
                return last
        except (requests.ConnectionError, requests.Timeout) as error:
            if not startup:
                raise
            last = str(error)
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}; last observation: {last!r}")


class _DevnetNode:
    """Use native configuration/RPC, with bounded cleanup of our own process only."""

    def __init__(self, directory, runtime, spec, filtered_heights, database=None, rich=False):
        self.directory = directory
        self.runtime = runtime
        self.rich = rich
        self.process = None
        self.log = None
        directory.mkdir()
        (directory / "dev.toml").write_text(toml.dumps(spec))
        options = FIXTURES / "default.db-options"
        (directory / "default.db-options").write_bytes(options.read_bytes())
        self.rpc_port = _port()
        self.p2p_port = _port()
        while self.p2p_port == self.rpc_port:
            self.p2p_port = _port()
        config = copy.deepcopy(CKB_DEFAULT_CONFIG)
        config.update({
            "ckb_rpc_listen_address": f"127.0.0.1:{self.rpc_port}",
            "ckb_network_listen_addresses": [f"/ip4/127.0.0.1/tcp/{self.p2p_port}"],
            "ckb_network_max_outbound_peers": 0,
            "ckb_logger_color": "false",
            "ckb_sentry_dsn": "",
        })
        if rich:
            config["ckb_rpc_modules"].append("RichIndexer")
        create_config_file(config, "suites/rpc/fixtures/ckb.toml.j2", str(directory / "ckb.toml"))
        rendered = toml.loads((directory / "ckb.toml").read_text())
        if rich:
            # 区块过滤保持固定，交易池叠加关闭。 / Keep a fixed block filter and disable pool overlay.
            indexer = {"index_tx_pool": False, "request_limit": 100}
            if filtered_heights:
                conditions = [f'block.header.number.to_uint() != "{hex(h)}".to_uint()'
                              for h in filtered_heights]
                indexer["block_filter"] = " && ".join(conditions)
            if database is None:
                indexer["rich_indexer"] = {
                    "db_type": "sqlite", "store": str(directory / "data/rich-indexer.sqlite")
                }
            else:
                postgres = runtime["postgres"]
                indexer["rich_indexer"] = {
                    "db_type": "postgres", "db_name": database,
                    "db_host": postgres["host"], "db_port": postgres["port"],
                    "db_user": postgres["user"], "db_password": postgres["password"],
                }
            rendered["indexer_v2"] = indexer
        self.config_file = directory / "ckb.toml"
        self.config_file.write_text(toml.dumps(rendered))
        self.client = RPCClient(f"http://127.0.0.1:{self.rpc_port}")

    def rpc(self, method, params=None):
        return self.client.call(method, [] if params is None else params,
                                try_count=1, timeout=5, verbose=False)

    def start(self):
        assert self.process is None, "Node was already started"
        self.log = (self.directory / "node.log").open("ab")
        arguments = [self.runtime["ckb_binary"], "run", "--skip-spec-check"]
        if self.rich:
            arguments.append("--rich-indexer")
        self.process = subprocess.Popen(arguments, cwd=self.directory,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        _wait("node RPC readiness", lambda: self.rpc("get_tip_header"), [self], startup=True)

    def assert_running(self):
        assert self.process is not None, "Node process was not started"
        if self.process.poll() is not None:
            tail = (self.directory / "node.log").read_text(errors="replace")[-3000:]
            raise AssertionError(f"Owned CKB process exited {self.process.returncode}: {tail}")

    def stop(self, normal=False):
        process = self.process
        if process is None:
            return
        forced = False
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
                process.wait(timeout=5)
        self.process = None
        self.log.close()
        self.log = None
        if normal:
            assert not forced, "Normal restart required forced process termination"
            assert process.returncode == 0, f"Normal shutdown returned {process.returncode}"

    def tip(self):
        return self.rpc("get_tip_header")

    def wait_indexed(self, block):
        target = {"block_hash": block["header"]["hash"],
                  "block_number": block["header"]["number"]}
        _wait(f"exact rich-indexer tip {target}",
              lambda: self.rpc("get_indexer_tip") == target, [self])
        assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"

    def mine(self, transactions=(), proposals=()):
        # 原生测试 RPC 重新计算 DAO 并验证区块；不绕过共识验证。
        # The native RPC recalculates DAO and verifies the resulting block.
        previous = self.tip()
        _wait("transaction pool at current canonical tip",
              lambda: self.rpc("tx_pool_info")["tip_hash"] == previous["hash"], [self])
        template = self.rpc("get_block_template")
        assert template["parent_hash"] == previous["hash"]
        template["transactions"] = [dict(hash=tx_hash, required=True, cycles=None,
                                         depends=None, data=tx) for tx_hash, tx in transactions]
        template["proposals"] = list(proposals)
        template["uncles"] = []
        template["current_time"] = hex(max(int(template["current_time"], 16),
                                            int(previous["timestamp"], 16) + 1))
        block_hash = self.rpc("generate_block_with_template", [template])
        block = self.rpc("get_block", [block_hash])
        assert block["header"]["parent_hash"] == previous["hash"]
        assert int(block["header"]["number"], 16) == int(previous["number"], 16) + 1
        assert [tx["hash"] for tx in block["transactions"][1:]] == [h for h, _ in transactions]
        assert self.tip()["hash"] == block_hash
        return block

    def import_block(self, block):
        submitted = copy.deepcopy(block)
        submitted["header"].pop("hash")
        for transaction in submitted["transactions"]:
            transaction.pop("hash")
        block_hash = self.rpc("submit_block", ["0x0", submitted])
        assert block_hash == block["header"]["hash"], "Imported block changed its hash"


class _RollbackScenario:
    """Explicit shared ancestry and complete public-query oracles for all six cases."""

    def __init__(self, backend, directory, old_order):
        self.backend = backend
        self.directory = directory
        self.old_order = old_order
        self.filtered_heights = [COMMON_HEIGHT + index + 1
                                 for index, kind in enumerate(old_order) if kind == "filtered"]
        self.runtime = _runtime()
        self.database = None
        self.nodes = []
        self.old_transactions = []
        self.filtered_transactions = []
        self.old_blocks = []
        self.new_blocks = []

    def __enter__(self):
        try:
            if self.backend == "postgres":
                postgres = self.runtime["postgres"]
                assert postgres["host"] == "127.0.0.1", "Only prepared loopback PostgreSQL is allowed"
                self.database = "ckb5300_" + uuid.uuid4().hex
                self._database_sql("CREATE DATABASE")
            spec = toml.loads((FIXTURES / "dev.toml").read_text())
            contract = FIXTURES / "always_success"
            self.code = contract.read_bytes()
            self.code_hash = "0x" + hashlib.blake2b(self.code, digest_size=32,
                                                   person=b"ckb-default-hash").hexdigest()
            spec["genesis"]["system_cells"].append({
                "file": {"file": str(contract)}, "create_type_id": False, "capacity": 1000 * CKB
            })
            self.lock = dict(code_hash=self.code_hash, hash_type="data", args="0x726f6c6c6261636b")
            self.auxiliary_lock = dict(self.lock, args="0x617578696c69617279")
            self.genesis_capacities = {"X": 1000 * CKB, "S": 700 * CKB, "P": 600 * CKB,
                                       "D": 1300 * CKB, "F1": 800 * CKB,
                                       "F2": 900 * CKB, "F3": 1100 * CKB}
            for name, capacity in self.genesis_capacities.items():
                lock = self.lock if name in ("X", "S", "P") else self.auxiliary_lock
                spec["genesis"]["issued_cells"].append({
                    "capacity": capacity, "lock": lock, "data": "0x" + name.encode().hex()
                })
            self.old = _DevnetNode(self.directory / "indexed-node", self.runtime, spec,
                                   self.filtered_heights, self.database, rich=True)
            self.nodes.append(self.old)
            self.fork = _DevnetNode(self.directory / "fork-node", self.runtime, spec, [])
            self.nodes.append(self.fork)
            self.old.start()
            self.fork.start()
            self._prepare_common_ancestor()
            return self
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, exception_type, exception, traceback):
        self._cleanup()

    def _database_sql(self, statement):
        import psycopg
        from psycopg import sql

        postgres = self.runtime["postgres"]
        with psycopg.connect(host=postgres["host"], port=postgres["port"],
                             user=postgres["user"], password=postgres["password"],
                             dbname="postgres", connect_timeout=5, autocommit=True) as connection:
            connection.execute(sql.SQL(statement + " {}").format(sql.Identifier(self.database)))

    def _cleanup(self):
        errors = []
        for node in reversed(self.nodes):
            try:
                node.stop()
            except Exception as error:
                errors.append(error)
        if self.database is not None:
            try:
                self._database_sql("DROP DATABASE")
                self.database = None
            except Exception as error:
                errors.append(error)
        assert not errors, f"Owned devnet/database cleanup failed: {errors}"

    def _cell(self, tx_hash, index, transaction, block, tx_index):
        return {"out_point": {"tx_hash": tx_hash, "index": hex(index)},
                "output": transaction["outputs"][index],
                "output_data": transaction["outputs_data"][index],
                "block_number": block["header"]["number"], "tx_index": hex(tx_index)}

    def _transaction(self, inputs, output_lock, marker):
        capacity = sum(int(cell["output"]["capacity"], 16) for cell in inputs) - FEE
        return {"version": "0x0", "cell_deps": [{"out_point": self.contract_outpoint,
                                                    "dep_type": "code"}],
                "header_deps": [],
                "inputs": [{"since": "0x0", "previous_output": cell["out_point"]}
                           for cell in inputs],
                "outputs": [{"capacity": hex(capacity), "lock": output_lock, "type": None}],
                "outputs_data": ["0x" + marker.encode().hex()], "witnesses": []}

    def _submit(self, node, transaction):
        tx_hash = node.rpc("send_transaction", [transaction, "passthrough"])
        assert tx_hash.startswith("0x") and len(tx_hash) == 66
        return tx_hash, transaction

    def _common_block(self, transactions=(), proposals=()):
        block = self.old.mine(transactions, proposals)
        self.fork.import_block(block)
        assert self.fork.tip()["hash"] == block["header"]["hash"]
        return block

    def _prepare_common_ancestor(self):
        genesis_hash = self.old.rpc("get_block_hash", ["0x0"])
        assert genesis_hash == self.fork.rpc("get_block_hash", ["0x0"])
        genesis = self.old.rpc("get_block", [genesis_hash])
        self.old.wait_indexed(genesis)
        genesis_transaction = genesis["transactions"][0]
        contract_indexes = [i for i, data in enumerate(genesis_transaction["outputs_data"])
                            if data == "0x" + self.code.hex()]
        assert len(contract_indexes) == 1, "Always-success system cell is not in the actual genesis"
        self.contract_outpoint = {"tx_hash": genesis_transaction["hash"], "index": hex(contract_indexes[0])}
        self.cells = {}
        for name in self.genesis_capacities:
            matching = [i for i, data in enumerate(genesis_transaction["outputs_data"])
                        if data == "0x" + name.encode().hex()]
            assert len(matching) == 1, f"Missing unique genesis cell {name}"
            cell = self._cell(genesis_transaction["hash"], matching[0], genesis_transaction, genesis, 0)
            assert int(cell["output"]["capacity"], 16) == self.genesis_capacities[name]
            assert cell["output"]["lock"] == (self.lock if name in ("X", "S", "P") else self.auxiliary_lock)
            assert self.old.rpc("get_live_cell", [cell["out_point"], True])["status"] == "live"
            self.cells[name] = cell
        self.genesis_history = self._history(genesis_transaction["hash"], genesis, 0,
                                              [("output", int(self.cells[n]["out_point"]["index"], 16))
                                               for n in ("X", "S", "P")])

        # P 与 X/S 使用相同查询锁，在共同祖先中先花费；两条分叉均不再涉及 P。
        # Spend P under the same query lock in retained ancestry; neither fork touches it again.
        ancestor_tx = self._submit(self.old, self._transaction([self.cells["P"]], self.lock, "Q"))
        self._common_block(proposals=[ancestor_tx[0][:22]])
        self._common_block()
        ancestor_block = self._common_block([ancestor_tx])
        self.cells["Q"] = self._cell(ancestor_tx[0], 0, ancestor_tx[1], ancestor_block, 1)
        self.ancestor_history = self._history(ancestor_tx[0], ancestor_block, 1,
                                                [("input", 0), ("output", 0)])

        self.new_transactions = []
        filtered_count = 0
        for kind in self.old_order:
            if kind == "indexed":
                self.old_spend = self._submit(self.old, self._transaction(
                    [self.cells["X"], self.cells["D"]], self.lock, "Y"))
                self.old_transactions.append(self.old_spend)
            else:
                filtered_count += 1
                name = f"F{filtered_count}"
                old_tx = self._submit(self.old, self._transaction([self.cells[name]], self.lock,
                                                                  f"filtered-{name}"))
                self.old_transactions.append(old_tx)
                self.filtered_transactions.append(old_tx)
                self.new_transactions.append(self._submit(self.fork, self._transaction(
                    [self.cells[name]], self.auxiliary_lock, f"competing-{name}")))
        # 竞争花费独立输入 D，使 X+ D 的旧交易不能重收录，而 X 可在回滚后再次花费。
        # A competing spend of D invalidates the detached X+D transaction while preserving X.
        self.new_spend = self._submit(self.fork, self._transaction([self.cells["D"]], self.lock, "N"))
        self.new_transactions.insert(0, self.new_spend)
        proposals = [tx_hash[:22] for tx_hash, _ in self.old_transactions + self.new_transactions]
        self._common_block(proposals=proposals)
        self.common = self._common_block()
        assert int(self.common["header"]["number"], 16) == COMMON_HEIGHT
        self.old.wait_indexed(self.common)
        self.retained_history = [self.genesis_history, self.ancestor_history]
        self.retained_cells = [self.cells[n] for n in ("X", "S", "Q")]
        self.assert_state(self.common, self.retained_cells, self.retained_history)
        assert self.old.rpc("get_live_cell", [self.cells["P"]["out_point"], True])["status"] != "live"

    @staticmethod
    def _history(tx_hash, block, tx_index, pairs):
        return {"tx_hash": tx_hash, "block_number": int(block["header"]["number"], 16),
                "tx_index": tx_index, "cells": sorted(pairs)}

    @staticmethod
    def _normalize_cell(cell):
        return {"out_point": (cell["out_point"]["tx_hash"], int(cell["out_point"]["index"], 16)),
                "output": cell["output"], "output_data": cell["output_data"],
                "block_number": int(cell["block_number"], 16), "tx_index": int(cell["tx_index"], 16)}

    def _pages(self, method, search_key):
        objects = []
        cursor = None
        seen_cursors = set()
        for _ in range(30):
            page = self.old.rpc(method, [search_key, "asc", hex(PAGE_SIZE), cursor])
            assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
            objects.extend(page["objects"])
            if len(page["objects"]) < PAGE_SIZE:
                return objects
            cursor = page["last_cursor"]
            assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
            seen_cursors.add(cursor)
        raise AssertionError(f"{method} did not terminate within 30 pages")

    def snapshot(self, block):
        self.old.wait_indexed(block)
        expected_hash = block["header"]["hash"]
        cells_key = {"script": self.lock, "script_type": "lock", "script_search_mode": "exact",
                     "with_data": True}
        tx_key = dict(cells_key, group_by_transaction=True)
        cells = [self._normalize_cell(cell) for cell in self._pages("get_cells", cells_key)]
        transactions = [{"tx_hash": tx["tx_hash"], "block_number": int(tx["block_number"], 16),
                         "tx_index": int(tx["tx_index"], 16),
                         "cells": sorted((kind, int(index, 16)) for kind, index in tx["cells"])}
                        for tx in self._pages("get_transactions", tx_key)]
        assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
        assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
        for transaction in transactions:
            assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
        capacity = self.old.rpc("get_cells_capacity", [cells_key])
        assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
        assert capacity["block_hash"] == expected_hash
        assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
        tip = self.old.rpc("get_indexer_tip")
        assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
            "Canonical/indexed tip moved while reading the fixed-tip snapshot"
        return {"tip": tip, "cells": sorted(cells, key=lambda cell: cell["out_point"]),
                "transactions": sorted(transactions, key=lambda tx: tx["tx_hash"]),
                "capacity": capacity}

    def assert_state(self, block, live_cells, history):
        state = self.snapshot(block)
        expected_cells = sorted((self._normalize_cell(cell) for cell in live_cells),
                                key=lambda cell: cell["out_point"])
        assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
        assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
            "Complete grouped transaction history/IO pairs differ from the expected retained history"
        expected_capacity = sum(int(cell["output"]["capacity"], 16) for cell in live_cells)
        assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
        p_outpoint = (self.cells["P"]["out_point"]["tx_hash"], int(self.cells["P"]["out_point"]["index"], 16))
        assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
        assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
        return state

    def index_old_fork(self):
        expected_cells = list(self.retained_cells)
        history = list(self.retained_history)
        for kind, old_tx in zip(self.old_order, self.old_transactions):
            block = self.old.mine([old_tx])
            self.old_blocks.append(block)
            # 每个过滤块在回滚前已到精确 hash；交易确实在节点块内。
            # Prove every filtered block was processed at its exact hash before rollback.
            self.old.wait_indexed(block)
            assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
            if kind == "indexed":
                self.cells["Y"] = self._cell(old_tx[0], 0, old_tx[1], block, 1)
                expected_cells = [self.cells[n] for n in ("S", "Q", "Y")]
                history.append(self._history(old_tx[0], block, 1, [("input", 0), ("output", 0)]))
            state = self.assert_state(block, expected_cells, history)
            for filtered_hash, _ in self.filtered_transactions:
                assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
        self.old_tip = self.old_blocks[-1]
        if "indexed" in self.old_order:
            assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
            assert self.old.rpc("get_live_cell", [self.cells["Y"]["out_point"], True])["status"] == "live"

    def adopt_new_fork(self):
        assert self.fork.tip()["hash"] == self.common["header"]["hash"]
        first_included = max(self.filtered_heights, default=COMMON_HEIGHT) + 1
        target_height = int(self.old_tip["header"]["number"], 16) + 3
        while int(self.fork.tip()["number"], 16) < target_height:
            height = int(self.fork.tip()["number"], 16) + 1
            transactions = self.new_transactions if height == first_included else []
            block = self.fork.mine(transactions)
            self.new_blocks.append(block)
            if transactions:
                self.cells["N"] = self._cell(self.new_spend[0], 0, self.new_spend[1], block, 1)
                self.new_history = self._history(self.new_spend[0], block, 1, [("output", 0)])
        assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
        self.new_tip = self.new_blocks[-1]
        assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
        # Dummy-PoW 的固定难度使更长分叉具有更大累计工作量。
        # Permanent Dummy-PoW difficulty makes this longer fork strictly stronger.
        for block in self.new_blocks:
            self.old.import_block(block)
        _wait("adoption of the stronger fork at its exact hash",
              lambda: self.old.tip()["hash"] == self.new_tip["header"]["hash"], [self.old])
        assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
        canonical = self.old.rpc("get_block_by_number", [self.new_tip["header"]["number"]])
        assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
        for transaction in (tx for block in self.new_blocks for tx in block["transactions"]):
            assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
            for item in transaction["inputs"]:
                assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
                    "New fork consumed a cell that must remain untouched"
        self.restored_cells = self.retained_cells + [self.cells["N"]]
        self.restored_history = self.retained_history + [self.new_history]
        state = self.assert_state(self.new_tip, self.restored_cells, self.restored_history)
        assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
        for old_hash, _ in self.old_transactions:
            assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
            assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
        return state

    def spend_restored_x(self):
        # 调用者先核对恢复快照；此后才提交 X -> Z，并继续出块。
        # Callers verify recovery first; only then submit X -> Z and continue mining.
        new_tx = self._submit(self.old, self._transaction([self.cells["X"]], self.lock, "Z"))
        self.old.mine(proposals=[new_tx[0][:22]])
        self.old.mine()
        committed = self.old.mine([new_tx])
        self.cells["Z"] = self._cell(new_tx[0], 0, new_tx[1], committed, 1)
        self.old.mine()
        final = self.old.mine()
        assert int(final["header"]["number"], 16) > int(committed["header"]["number"], 16)
        expected_history = self.restored_history + [self._history(new_tx[0], committed, 1,
                                                                  [("input", 0), ("output", 0)])]
        state = self.assert_state(final, [self.cells[n] for n in ("S", "Q", "N", "Z")], expected_history)
        assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
        assert self.old.rpc("get_live_cell", [self.cells["Z"]["out_point"], True])["status"] == "live"
        for old_hash, _ in self.old_transactions:
            assert old_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]


class TestRichIndexerRollback:
    # TEST-MAP: RICH-ROLLBACK-01
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-01
    # Evidence | Case: RICH-ROLLBACK-01 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 旧高度 6 的一个真实 F1 交易块被整块过滤。 / EN: One real F1 transaction block at old height 6 is block-filtered.
    # Evidence | Trigger: 撤销一个已处理但无索引交易的旧块，随后追到过滤范围之外的新链。 / EN: Revoke one processed block with no indexed transactions and catch up beyond the filtered range. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 切换前逐块证明节点块内存在 F1 交易且索引精确到该 hash，但交易不在完整分组结果；切换后 X/S/Q/N 与祖先及 N 历史精确相符。 / EN: Before adoption, prove F1 inclusion in the node block and the exact indexed hash while complete grouped results exclude it. Afterwards X/S/Q/N and retained/N history match exactly.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-01
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    def test_single_filtered_block(self, backend, tmp_path):
        with _RollbackScenario(backend, tmp_path, ["filtered"]) as scenario:
            scenario.index_old_fork()
            scenario.adopt_new_fork()

    # TEST-MAP: RICH-ROLLBACK-02
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-02
    # Evidence | Case: RICH-ROLLBACK-02 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 旧高度 6/7/8 的三个真实 F1/F2/F3 交易块连续被过滤。 / EN: Three real F1/F2/F3 transaction blocks at old heights 6/7/8 are consecutively filtered.
    # Evidence | Trigger: 撤销三个已经逐块观察到精确 tip 的空索引交易块，随后收录过滤范围之外的新交易。 / EN: Revoke three empty-indexed blocks whose exact tips were observed individually, then index new transactions beyond the filter range. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 每个旧块都比较完整过滤状态；最终 X/S/Q/N、P 非 live、祖先及 N 分组历史和容量均核对。 / EN: Compare complete filtered state at every old block; finally verify X/S/Q/N, non-live P, retained/N grouped history and capacity.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-02
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    def test_consecutive_filtered_blocks(self, backend, tmp_path):
        with _RollbackScenario(backend, tmp_path, ["filtered", "filtered", "filtered"]) as scenario:
            scenario.index_old_fork()
            scenario.adopt_new_fork()

    # TEST-MAP: RICH-ROLLBACK-03
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-03
    # Evidence | Case: RICH-ROLLBACK-03 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 不启用 block filter，旧高度 6 的交易消费 X+D 创建 Y。 / EN: No block filter is enabled; the old height-6 transaction spends X+D into Y.
    # Evidence | Trigger: 先比较旧 S/Q/Y 和旧交易 input 0/output 0，并用节点 live-cell 查询证明 X 非 live、Y live，然后切换至保留 X 的新链。 / EN: Compare old S/Q/Y and old grouped input 0/output 0, prove X non-live and Y live through node RPC, then adopt the fork preserving X. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 切换后完整索引集合恢复 X，排除 Y/旧交易并保留 S/Q/N、P 非 live 和祖先历史；节点 live-cell RPC 独立证明 X live。 / EN: Complete index results restore X, exclude Y/old transaction and retain S/Q/N, non-live P and ancestor history; node live-cell RPC independently proves live X.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Y"]["out_point"], True])["status"] == "live"
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-03
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    def test_indexed_spend_restored(self, backend, tmp_path):
        with _RollbackScenario(backend, tmp_path, ["indexed"]) as scenario:
            scenario.index_old_fork()
            scenario.adopt_new_fork()

    # TEST-MAP: RICH-ROLLBACK-04
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-04
    # Evidence | Case: RICH-ROLLBACK-04 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 两种数据库各配两种 rollback_order；filtered-first 构造 indexed→filtered，indexed-first 构造 filtered→indexed，实际回滚从 tip 向祖先反序进行。 / EN: Both backends use both rollback_order parameters: filtered-first constructs indexed→filtered and indexed-first constructs filtered→indexed; rollback reverses construction from tip to ancestry.
    # Evidence | Trigger: 逐旧块核对过滤与 X→Y 状态，再切换主链，分别触发空列表先于非空和非空先于空列表的恢复。 / EN: Verify filtering and X→Y state at each old block, then adopt the canonical fork to exercise empty-before-nonempty and nonempty-before-empty rollback. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 四个参数均由同一完整公共查询 oracle 核对 X 恢复、Y/旧交易删除、S/Q/N 保留、P 非 live、祖先历史及容量。 / EN: All four concrete parameters use the complete public-query oracle to verify restored X, removed Y/old transaction, retained S/Q/N, non-live P, ancestor history and capacity.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Y"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-04
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    @pytest.mark.parametrize("rollback_order", ["filtered-first", "indexed-first"],
                             ids=["filtered-first", "indexed-first"])
    def test_mixed_rollback_orders(self, backend, rollback_order, tmp_path):
        old_order = (["indexed", "filtered"] if rollback_order == "filtered-first"
                     else ["filtered", "indexed"])
        # 回滚按 tip -> ancestor；旧链的构造顺序与实际回滚顺序相反。
        # Rollback visits tip -> ancestor, reversing the old chain's construction order.
        with _RollbackScenario(backend, tmp_path, old_order) as scenario:
            scenario.index_old_fork()
            scenario.adopt_new_fork()

    # TEST-MAP: RICH-ROLLBACK-05
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-05
    # Evidence | Case: RICH-ROLLBACK-05 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 使用 indexed→filtered 旧链，并先完成恢复快照。 / EN: Use an indexed→filtered old chain and first verify the restored snapshot.
    # Evidence | Trigger: 在观察到 X 恢复、Y/旧交易退出后才提交 X→Z，经过提案窗口在允许收录的区块提交交易，再继续生成两个块。 / EN: Only after observing restored X and removed Y/old transaction, submit X→Z, commit it after the proposal window in an included block, and mine two more blocks. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 新的固定 tip 下完整 live 集合为 S/Q/N/Z；分组历史新增 X→Z 的 input 0/output 0，X 既有历史保留，容量按新集合计算，节点 RPC 证明 X 非 live、Z live且旧交易仍不存在。 / EN: At the new fixed tip the complete live set is S/Q/N/Z; grouped history adds X→Z input 0/output 0 while retaining X history, capacity follows the new set, and node RPC proves X non-live/Z live with detached transactions absent.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Y"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Assert: assert int(final["header"]["number"], 16) > int(committed["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Z"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert old_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-05
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    def test_spend_restored_cell(self, backend, tmp_path):
        with _RollbackScenario(backend, tmp_path, ["indexed", "filtered"]) as scenario:
            scenario.index_old_fork()
            scenario.adopt_new_fork()
            scenario.spend_restored_x()

    # TEST-MAP: RICH-ROLLBACK-06
    # TEST-EVIDENCE-BEGIN: RICH-ROLLBACK-06
    # Evidence | Case: RICH-ROLLBACK-06 (covered)
    # Evidence | Setup: 每个数据库参数创建两个真实本地 Dummy-PoW 节点，PostgreSQL 使用唯一数据库，SQLite 使用临时节点目录内的独立文件。通过原生配置模板、真实 RPC 与创世块查询建立 always-success 合约和容量不同的 X/S/P/D/F cell；共同祖先高度为 5，先消费同一精确 lock 下的 P 创建 Q，并核对完整 X/S/Q、祖先交易 IO 和容量。关闭 txpool overlay，未 mock 节点或 SQL 查询。 / EN: Each backend parameter creates two real local Dummy-PoW nodes, a unique PostgreSQL database or isolated SQLite file. Native configuration and RPC plus actual genesis queries establish an always-success contract and distinct-capacity X/S/P/D/F cells. At shared height 5, P under the same exact lock was spent into Q; complete X/S/Q, retained ancestor IO and capacity are checked. Pool overlay is disabled and node/SQL queries are not mocked. 使用 indexed→filtered 旧链完成回滚，保存完整快照并保留同一节点对象、目录、数据库与配置字节，固定链头。 / EN: Complete indexed→filtered rollback, save the complete snapshot and retain the same node object, directory, database and configuration bytes at a fixed tip.
    # Evidence | Trigger: 正常停止要求不强杀且退出码为零；启动原节点且不清库，先比较重启前后快照，再执行 X→Z 和后续出块。 / EN: Normal stop requires no forced kill and zero exit code; restart the original node without clearing storage, compare snapshots first, then execute X→Z and further mining. 先处理并查询旧分叉的每个目标块，再逐块导入从共同祖先生成、比旧 tip 高 3 块的分叉。新链通过 D 和有关 F 的竞争花费排除旧交易，在过滤高度之外创建 N。核对首块分叉 hash 不同、旧高度主链 hash 已改变、新目标块确为节点主链，且新链不消费 X/P/S。 / EN: Process and query each old-fork target block before importing a fork built from shared ancestry, three blocks beyond the old tip. Competing D/relevant F spends exclude detached transactions, and N is created beyond filtered heights. Assertions prove divergent first hashes, replacement at the old canonical height, actual adoption of the new target, and untouched X/P/S.
    # Evidence | Observe: 等待 get_indexer_tip 与目标 hash/高度完全相等后重新查询精确 lock。get_cells 与 group_by_transaction=true 的 get_transactions 使用每页 2 项遍历到末页，拒绝重复 cursor、outpoint、交易和 IO 对。完整 cell 属性、分组历史/IO 与根据实际创世块和构造交易建立的预期比较；容量等于预期 live cell 的独立总和并绑定目标 hash/高度。P 不在 live 集合，消费 P 的合法祖先历史保留，旧分叉输出/交易退出；查询结束时再次核对固定链头。 / EN: Wait for exact indexed hash/height, then issue fresh exact-lock queries. Live cells and grouped transactions traverse pages of two to the final page, rejecting repeated cursors, outpoints, transactions and IO pairs. Complete cell fields and grouped history/IO match expectations built from genesis and constructed transactions; capacity equals the independent expected live-cell sum and carries the target hash/height. P stays non-live with its valid ancestor spend retained, detached outputs/transactions are absent, and the fixed tip is checked again after querying. 继续出块前断言目录/数据库、配置字节、节点 tip 不变，重新查询的完整 tip/cell/交易/容量快照等于先前快照；随后复用再次花费 oracle，核对 S/Q/N/Z、P 非 live、祖先 IO、新交易 IO、更新容量及确切新 tip。 / EN: Before mining resumes, assert retained directory/database, config bytes and node tip, and compare freshly queried complete tip/cells/transactions/capacity with the saved snapshot. Then reuse the later-spend oracle to verify S/Q/N/Z, non-live P, ancestor/new IO, updated capacity and exact advancing tip.
    # Evidence | Assert: assert self.tip()["hash"] == target["block_hash"], "Node and expected canonical block differ"
    # Evidence | Assert: _wait(f"exact rich-indexer tip {target}",
    # Evidence |               lambda: self.rpc("get_indexer_tip") == target, [self])
    # Evidence | Assert: assert state["cells"] == expected_cells, "Complete live cells differ from the canonical/filter expectation"
    # Evidence | Assert: assert state["transactions"] == sorted(history, key=lambda tx: tx["tx_hash"]), \
    # Evidence |             "Complete grouped transaction history/IO pairs differ from the expected retained history"
    # Evidence | Assert: assert int(state["capacity"]["capacity"], 16) == expected_capacity, "Live capacity includes a stale or wrongly restored cell"
    # Evidence | Assert: assert p_outpoint not in [cell["out_point"] for cell in state["cells"]], "Ancestor P was incorrectly restored"
    # Evidence | Assert: assert self.ancestor_history in state["transactions"], "Retained P-spend history was deleted"
    # Evidence | Assert: assert len({cell["out_point"] for cell in cells}) == len(cells), "Duplicate live outpoints"
    # Evidence | Assert: assert len({tx["tx_hash"] for tx in transactions}) == len(transactions), "Duplicate grouped transactions"
    # Evidence | Assert: assert capacity is not None, "Capacity result unavailable for nonempty retained cells"
    # Evidence | Assert: assert capacity["block_hash"] == expected_hash
    # Evidence | Assert: assert int(capacity["block_number"], 16) == int(block["header"]["number"], 16)
    # Evidence | Assert: assert tip["block_hash"] == expected_hash and self.old.tip()["hash"] == expected_hash, \
    # Evidence |             "Canonical/indexed tip moved while reading the fixed-tip snapshot"
    # Evidence | Assert: assert len(set(transaction["cells"])) == len(transaction["cells"]), "Duplicate grouped IO pairs"
    # Evidence | Assert: assert len(page["objects"]) <= PAGE_SIZE, f"{method} exceeded its requested page size"
    # Evidence | Assert: assert cursor not in seen_cursors, f"{method} repeated a pagination cursor"
    # Evidence | Assert: assert self.fork.tip()["hash"] == self.common["header"]["hash"]
    # Evidence | Assert: assert self.old_blocks[0]["header"]["hash"] != self.new_blocks[0]["header"]["hash"], "Forks did not diverge"
    # Evidence | Assert: assert int(self.new_tip["header"]["number"], 16) > int(self.old_tip["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_block_hash", [self.old_tip["header"]["number"]]) != self.old_tip["header"]["hash"]
    # Evidence | Assert: assert canonical["header"]["hash"] == self.new_tip["header"]["hash"]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert transaction["hash"] not in [tx_hash for tx_hash, _ in self.old_transactions], "Detached transaction was re-included"
    # Evidence | Assert: assert old_hash not in [tx["tx_hash"] for tx in state["transactions"]], "Stale old-fork transaction remains indexed"
    # Evidence | Assert: assert (old_hash, 0) not in [cell["out_point"] for cell in state["cells"]], "Stale old-fork output remains live"
    # Evidence | Assert: assert item["previous_output"] not in [self.cells[n]["out_point"] for n in ("X", "P", "S")], \
    # Evidence |                     "New fork consumed a cell that must remain untouched"
    # Evidence | Assert: assert old_tx[0] in [transaction["hash"] for transaction in block["transactions"]]
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Y"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert filtered_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Assert: assert int(final["header"]["number"], 16) > int(committed["header"]["number"], 16)
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["X"]["out_point"], True])["status"] != "live"
    # Evidence | Assert: assert self.old.rpc("get_live_cell", [self.cells["Z"]["out_point"], True])["status"] == "live"
    # Evidence | Assert: assert old_hash not in [transaction["tx_hash"] for transaction in state["transactions"]]
    # Evidence | Assert: assert scenario.old.directory == directory and scenario.database == database
    # Evidence | Assert: assert scenario.old.config_file.read_bytes() == config_bytes, "Restart changed node configuration"
    # Evidence | Assert: assert scenario.old.tip()["hash"] == scenario.new_tip["header"]["hash"], "Restart moved the fixed chain tip"
    # Evidence | Assert: assert after == before, "Retained-storage restart changed the complete fixed-tip snapshot"
    # Evidence | Assert: assert not forced, "Normal restart required forced process termination"
    # Evidence | Assert: assert process.returncode == 0, f"Normal shutdown returned {process.returncode}"
    # Evidence | Limit: 覆盖只涉及固定精确 lock、有限构造的 cell/交易、固定高度 block filter 和分组交易查询；完整结果指该查询范围。有限深度与输入不能证明所有回滚深度、所有输入或整个数据库。 / EN: Coverage concerns one fixed exact lock, finite constructed cells/transactions, fixed height filtering and grouped queries; complete results refer to that query domain. Finite depths/inputs do not establish all rollback depths, all inputs or whole-database correctness.
    # Evidence | Limit: cell filtering、txpool overlay、非分组分页、数据库中断、强制崩溃、迁移、性能与其他产品 PR 不在本 slice；未计量 reset_spent_cells 的内部数据库调用次数，接口正确不能证明零内部查询。 / EN: Cell filtering, pool overlay, ungrouped pagination, outages, forced crashes, migrations, performance and other product PRs are outside this slice. Internal reset_spent_cells database-call counts are not measured; correct public behavior does not establish zero internal queries.
    # Evidence | Limit: 等待使用 45 秒 deadline、0.1 秒轮询和每次 RPC 的单次调用/5 秒超时；仅启动阶段重试连接或超时。其他 RPC 异常、节点退出、过期等待和清理失败使测试失败，不被吞掉。分页最多 30 页；这些有限条件不是性能阈值保证。 / EN: Waits use a 45-second deadline, 0.1-second polling and one RPC attempt with a five-second timeout; connection/timeouts are retried only at startup. Other RPC errors, node exits, expired waits and cleanup failures fail the test. Pagination allows at most 30 pages; these finite conditions are not performance guarantees.
    # Evidence | Limit: 记录器在 logreport 时浅拷贝 TestReport，判定仅依赖 when/outcome/wasxfail 标量；HTML 合并阶段对原报告的标量重赋值不改写快照。只有完整且唯一的 setup/call/teardown 成功才授予执行成功；失败、跳过、重复阶段及意外成功保留失败或不稳定信息。该规则不保证所有其他插件、嵌套可变字段或并发 runner 的兼容性；Case 覆盖判断与正式执行证据分别管理。 / EN: The recorder shallow-copies TestReport at logreport and classifies scalar when/outcome/wasxfail fields; later HTML scalar reassignment leaves snapshots intact. Execution success requires complete unique successful setup/call/teardown phases; failures, skips, duplicate phases and unexpected successes retain failure or instability information. This does not guarantee compatibility with every other plugin, nested mutable field or concurrent runner; Case coverage and official execution evidence remain separate.
    # Evidence | Limit: 重启覆盖正常停止后原存储的接口一致性和继续索引；不通过 crash 或故障注入证明 durability，也不计量启动内部重放次数。 / EN: Restart coverage checks interface consistency and continued indexing after normal stop with retained storage; it does not prove crash durability through fault injection or count internal startup replay operations.
    # TEST-EVIDENCE-END: RICH-ROLLBACK-06
    @pytest.mark.parametrize("backend", BACKENDS, ids=BACKENDS)
    def test_restart_preserves_rollback(self, backend, tmp_path):
        with _RollbackScenario(backend, tmp_path, ["indexed", "filtered"]) as scenario:
            scenario.index_old_fork()
            before = scenario.adopt_new_fork()
            # 保留相同目录、SQL 数据库、配置和固定链头；快照一致后才再出块。
            # Retain directory, SQL database, config and fixed tip; compare before mining again.
            directory = scenario.old.directory
            config_bytes = scenario.old.config_file.read_bytes()
            database = scenario.database
            scenario.old.stop(normal=True)
            scenario.old.start()
            assert scenario.old.directory == directory and scenario.database == database
            assert scenario.old.config_file.read_bytes() == config_bytes, "Restart changed node configuration"
            assert scenario.old.tip()["hash"] == scenario.new_tip["header"]["hash"], "Restart moved the fixed chain tip"
            after = scenario.assert_state(scenario.new_tip, scenario.restored_cells, scenario.restored_history)
            assert after == before, "Retained-storage restart changed the complete fixed-tip snapshot"
            scenario.spend_restored_x()
