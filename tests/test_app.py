"""评估逻辑与 HTTP API 的端到端测试（仅标准库）。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from museum.evaluate import evaluate_artifact
from museum.riskgroups import find_risk_groups

BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)


def _ev(eid, aid, metric, s, e, node_ids, status="open"):
    """构造风险组算法输入（s/e 为相对 BASE 的分钟数）。"""
    def ts(m):
        return (BASE + timedelta(minutes=m)).isoformat()
    return {
        "id": eid, "artifact_id": aid, "artifact_name": f"文物{aid}",
        "metric": metric, "start_ts": ts(s), "end_ts": ts(e),
        "status": status,
        "nodes": [{"id": nid, "name": f"节点{nid}"} for nid in node_ids],
    }


def make_reading(minutes, temp, hum, node_id=1, node_name="节点", node_seq=1):
    return {
        "ts": BASE + timedelta(minutes=minutes),
        "temperature": temp,
        "humidity": hum,
        "node_id": node_id,
        "node_name": node_name,
        "node_seq": node_seq,
    }


ARTIFACT = {"temp_min": 18.0, "temp_max": 24.0, "hum_min": 45.0, "hum_max": 60.0,
            "sample_interval": 600}


class EvaluateTest(unittest.TestCase):
    def test_merge_across_nodes(self):
        """同一指标的连续越界读数跨节点合并为一个事件。"""
        readings = [
            make_reading(0, 20, 50, 1, "库房", 1),
            make_reading(10, 26, 50, 1, "库房", 1),   # 越界开始
            make_reading(20, 27, 50, 2, "运输车", 2),  # 跨节点仍连续
            make_reading(30, 25, 50, 3, "展厅", 3),
            make_reading(40, 21, 50, 3, "展厅", 3),   # 恢复正常
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(events), 1)
        self.assertEqual(gaps, [])
        ev = events[0]
        self.assertEqual(ev["metric"], "temperature")
        self.assertEqual(ev["reading_count"], 3)
        self.assertEqual([n["name"] for n in ev["nodes"]], ["库房", "运输车", "展厅"])
        self.assertAlmostEqual(ev["max_deviation"], 3.0)

    def test_gap_interrupts_event(self):
        """相邻读数间隔超过采样间隔：记缺测区间并中断事件。"""
        readings = [
            make_reading(0, 26, 50),    # 越界
            make_reading(10, 27, 50),   # 越界
            make_reading(60, 28, 50),   # 间隔 50 分钟 > 10 分钟 → 缺测，事件中断
            make_reading(70, 21, 50),
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["expected_interval"], 600)
        self.assertEqual(gaps[0]["actual_interval"], 3000)
        temp_events = [e for e in events if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)
        self.assertEqual(temp_events[0]["reading_count"], 2)
        self.assertEqual(temp_events[1]["reading_count"], 1)

    def test_single_outlier_is_event(self):
        readings = [make_reading(0, 20, 50), make_reading(10, 20, 75)]
        events, _ = evaluate_artifact(readings, ARTIFACT)
        hum = [e for e in events if e["metric"] == "humidity"]
        self.assertEqual(len(hum), 1)
        self.assertEqual(hum[0]["reading_count"], 1)

    def test_all_in_range(self):
        readings = [make_reading(i * 10, 21, 50) for i in range(5)]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(events, [])
        self.assertEqual(gaps, [])


class RiskGroupAlgorithmTest(unittest.TestCase):
    def test_basic_group_same_metric_overlap_shared_node(self):
        """同指标、闭区间相交、共享节点 → 成组。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1, 2]),
            _ev(2, 2, "temperature", 20, 60, [2, 3]),
        ])
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["artifact_count"], 2)
        self.assertEqual(g["event_count"], 2)
        self.assertEqual(g["open_events"], 2)
        self.assertEqual(g["closed_events"], 0)
        self.assertEqual({a["id"] for a in g["artifacts"]}, {1, 2})
        self.assertEqual([n["id"] for n in g["common_nodes"]], [2])
        self.assertEqual(len(g["links"]), 1)
        self.assertEqual(g["links"][0]["events"], [1, 2])
        self.assertEqual([n["id"] for n in g["links"][0]["shared_nodes"]], [2])

    def test_closed_interval_touching_endpoints_overlap(self):
        """闭区间：一个事件 end 恰好等于另一个 start → 相交。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 20, [1]),
            _ev(2, 2, "temperature", 20, 40, [1]),
        ])
        self.assertEqual(len(groups), 1)

    def test_different_metric_no_edge(self):
        """指标不同（温度 vs 湿度）即使时间重叠且共享节点也不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 2, "humidity", 10, 20, [1]),
        ])
        self.assertEqual(groups, [])

    def test_no_overlap_no_edge(self):
        """闭区间不相交（首尾错开）不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 10, [1]),
            _ev(2, 2, "temperature", 20, 30, [1]),
        ])
        self.assertEqual(groups, [])

    def test_overlap_but_no_shared_node_no_edge(self):
        """时间相交但无共同运输节点不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 2, "temperature", 10, 20, [2]),
        ])
        self.assertEqual(groups, [])

    def test_single_artifact_component_not_a_group(self):
        """连通分量只覆盖一件文物（即便有多个事件）不成组。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 1, "temperature", 20, 60, [1]),
        ])
        self.assertEqual(groups, [])

    def test_transitive_chain_includes_all_common_nodes_may_be_empty(self):
        """A-B 共享节点 1、B-C 共享节点 2，A-C 无共同节点：传递相连全纳入，
        全组共有节点为空，且不得误判 A-C 有共同节点。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 100, [1]),
            _ev(2, 2, "temperature", 0, 100, [1, 2]),
            _ev(3, 3, "temperature", 0, 100, [2]),
        ])
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["artifact_count"], 3)
        self.assertEqual(g["event_count"], 3)
        self.assertEqual(g["common_nodes"], [])  # 无全体共有节点
        pairs = {tuple(sorted(lk["events"])) for lk in g["links"]}
        self.assertEqual(pairs, {(1, 2), (2, 3)})  # (1,3) 不是连边

    def test_status_counts(self):
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1], status="closed"),
            _ev(2, 2, "temperature", 10, 40, [1], status="open"),
        ])
        g = groups[0]
        self.assertEqual(g["open_events"], 1)
        self.assertEqual(g["closed_events"], 1)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_flow(self):
        # 创建批次
        st, batch = self.req("POST", "/api/batches",
                             {"name": "2026 春季书画巡展", "description": "测试"})
        self.assertEqual(st, 201)
        bid = batch["id"]

        # 有序节点
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        # 重复节点序号 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/nodes",
                           {"seq": 1, "name": "重复"})
        self.assertEqual(st, 400)
        self.assertIn("已存在", err["error"])

        # 无序上下限 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "坏文物", "temp_min": 25, "temp_max": 18,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 400)
        self.assertIn("无序", json.dumps(err, ensure_ascii=False))

        # 正常文物
        st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "青花瓷瓶", "temp_min": 18, "temp_max": 24,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 201)
        aid = art["id"]
        _, detail = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in detail["nodes"]]

        # 非数值读数 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [{"artifact_id": aid, "node_id": n1,
                                          "ts": "2026-10-01T08:00:00Z",
                                          "temperature": "abc", "humidity": 50}]})
        self.assertEqual(st, 400)
        self.assertIn("非数值", json.dumps(err, ensure_ascii=False))

        # 第一批读数：温度在库房→运输车→展厅连续越界（跨节点合并），
        # 之后出现超过采样间隔的缺测，再出现一次越界
        readings = [
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:00:00Z",
             "temperature": 20, "humidity": 50},
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:10:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n2, "ts": "2026-10-01T08:20:00Z",
             "temperature": 27, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:30:00Z",
             "temperature": 25, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:40:00Z",
             "temperature": 21, "humidity": 50},
            # 缺测：08:40 → 09:40 间隔 60 分钟 > 10 分钟
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:40:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:50:00Z",
             "temperature": 21, "humidity": 50},
        ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "首批数据"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        temp_events = [e for e in res["events"] if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)  # 缺测中断 → 两个事件
        self.assertEqual(temp_events[0]["reading_count"], 3)
        self.assertEqual([n["name"] for n in temp_events[0]["nodes"]],
                         ["库房", "运输车", "展厅"])
        self.assertEqual(len(res["gaps"]), 1)

        # 重复时间戳（相同数值）→ 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [readings[0]]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 同一提交内重复时间戳 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 20, "humidity": 50},
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 22, "humidity": 55}]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 关闭事件：缺少处置说明/证据编号 → 拒绝
        ev_id = temp_events[0]["id"]
        st, err = self.req("POST", f"/api/events/{ev_id}/close", {"note": "已处置"})
        self.assertEqual(st, 400)
        self.assertIn("证据编号", err["error"])
        # 填写完整 → 关闭成功
        st, closed = self.req("POST", f"/api/events/{ev_id}/close",
                              {"note": "已调整车厢温控并复查", "evidence_no": "EV-2026-001"})
        self.assertEqual(st, 200)
        self.assertEqual(closed["status"], "closed")

        # 更正读数（同一时间戳、不同数值）→ 生成新版本
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings",
                            {"readings": [{"artifact_id": aid, "node_id": n2,
                                           "ts": "2026-10-01T08:20:00Z",
                                           "temperature": 22, "humidity": 50}],
                             "note": "更正运输车读数"})
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        self.assertEqual(res2["corrected"], 1)

        # 新版事件不继承关闭状态
        v2_events = res2["events"]
        self.assertTrue(all(e["status"] == "open" for e in v2_events))

        # 旧版读数与关闭结论保持可查
        st, v1 = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(st, 200)
        old_event = [e for e in v1["events"] if e["id"] == ev_id][0]
        self.assertEqual(old_event["status"], "closed")
        self.assertEqual(old_event["close_evidence"], "EV-2026-001")
        old_reading = [r for r in v1["readings"]
                       if r["ts"].startswith("2026-10-01T08:20")][0]
        self.assertEqual(old_reading["temperature"], 27)  # 旧值保留

        # 版本差异
        st, diff = self.req("GET", f"/api/batches/{bid}/diff?from=1&to=2")
        self.assertEqual(st, 200)
        self.assertEqual(len(diff["readings"]["corrected"]), 1)
        self.assertGreaterEqual(len(diff["events"]["closure_not_inherited"]), 1)

        # 历史版本事件不可再关闭
        st, err = self.req("POST", f"/api/events/{ev_id}/close",
                           {"note": "x", "evidence_no": "y"})
        self.assertEqual(st, 400)

        # 批次风险摘要
        st, summary = self.req("GET", f"/api/batches/{bid}/summary")
        self.assertEqual(st, 200)
        self.assertEqual(summary["current_version"], 2)
        self.assertEqual(summary["gaps"], 1)
        self.assertGreaterEqual(summary["open_events"], 1)

        # 页面可访问
        for path in ("/", f"/batches/{bid}", f"/batches/{bid}/versions/1",
                     f"/batches/{bid}/diff?from=1&to=2"):
            url = f"http://127.0.0.1:{self.port}{path}"
            with urllib.request.urlopen(url) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers["Content-Type"])

class RiskGroupApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test-rg.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_risk_groups_across_versions(self):
        st, batch = self.req("POST", "/api/batches", {"name": "共因风险测试批次"})
        self.assertEqual(st, 201)
        bid = batch["id"]
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            self.req("POST", f"/api/batches/{bid}/nodes",
                     {"seq": seq, "name": name})
        _, a1 = self.req("POST", f"/api/batches/{bid}/artifacts",
                         {"name": "文物甲", "temp_min": 18, "temp_max": 24,
                          "hum_min": 45, "hum_max": 60, "sample_interval": 3600})
        _, a2 = self.req("POST", f"/api/batches/{bid}/artifacts",
                         {"name": "文物乙", "temp_min": 18, "temp_max": 24,
                          "hum_min": 45, "hum_max": 60, "sample_interval": 3600})
        _, det = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in det["nodes"]]
        id1, id2 = a1["id"], a2["id"]

        def r(aid, node, ts, t, h=50):
            return {"artifact_id": aid, "node_id": node, "ts": ts,
                    "temperature": t, "humidity": h}

        # v1：两文物在运输车（n2）同期温度越界（08:10~08:30 闭区间相交）
        readings = [
            r(id1, n1, "2026-10-01T08:00:00Z", 20),
            r(id1, n2, "2026-10-01T08:10:00Z", 27),
            r(id1, n2, "2026-10-01T08:20:00Z", 27),
            r(id1, n3, "2026-10-01T08:30:00Z", 21),
            r(id2, n2, "2026-10-01T08:10:00Z", 26),
            r(id2, n2, "2026-10-01T08:20:00Z", 26),
            r(id2, n3, "2026-10-01T08:30:00Z", 21),
        ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "v1"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        v1_temp_events = [e for e in res["events"] if e["metric"] == "temperature"]
        self.assertEqual(len(v1_temp_events), 2)

        # 当前版风险组
        st, rg = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(st, 200)
        self.assertEqual(rg["version"], 1)
        self.assertEqual(len(rg["groups"]), 1)
        g = rg["groups"][0]
        self.assertEqual(g["artifact_count"], 2)
        self.assertEqual({a["id"] for a in g["artifacts"]}, {id1, id2})
        self.assertEqual([n["name"] for n in g["common_nodes"]], ["运输车"])
        self.assertEqual(g["open_events"], 2)
        self.assertEqual(g["closed_events"], 0)

        # 显式 version=1
        st, rg1 = self.req("GET", f"/api/batches/{bid}/risk-groups?version=1")
        self.assertEqual(st, 200)
        self.assertEqual(len(rg1["groups"]), 1)

        # 版本快照内含分组
        st, snap = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(len(snap["risk_groups"]), 1)

        # 关闭其中一个事件：只更新所属版本（当前版）状态计数，成员不变
        close_target = [e for e in v1_temp_events if e["artifact_id"] == id1][0]
        st, _ = self.req("POST", f"/api/events/{close_target['id']}/close",
                         {"note": "已处置", "evidence_no": "EV-X-1"})
        self.assertEqual(st, 200)
        st, rg_closed = self.req("GET", f"/api/batches/{bid}/risk-groups")
        g2 = rg_closed["groups"][0]
        self.assertEqual(g2["event_count"], 2)
        self.assertEqual(g2["open_events"], 1)
        self.assertEqual(g2["closed_events"], 1)

        # 补录/更正读数生成 v2：把文物甲运输车读数更正为正常值 → 共因消失，重算分组
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings", {
            "note": "更正文物甲运输车读数",
            "readings": [r(id1, n2, "2026-10-01T08:10:00Z", 21),
                         r(id1, n2, "2026-10-01T08:20:00Z", 21)],
        })
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        st, rg2 = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(rg2["version"], 2)
        self.assertEqual(rg2["groups"], [])

        # 旧版分组保持可查（含关闭后的计数）
        st, old = self.req("GET", f"/api/batches/{bid}/risk-groups?version=1")
        self.assertEqual(st, 200)
        self.assertEqual(len(old["groups"]), 1)
        self.assertEqual(old["groups"][0]["closed_events"], 1)

        # 错误：批次不存在 / 版本不存在（中文错误）
        st, err = self.req("GET", "/api/batches/9999/risk-groups")
        self.assertEqual(st, 404)
        self.assertIn("不存在", err["error"])
        st, err = self.req("GET", f"/api/batches/{bid}/risk-groups?version=99")
        self.assertEqual(st, 404)
        self.assertIn("版本 99 不存在", err["error"])
        st, err = self.req("GET", f"/api/batches/{bid}/versions/99/risk-groups")
        self.assertEqual(st, 404)
        self.assertIn("版本 99 不存在", err["error"])

        # 页面包含风险组区块
        for path in (f"/batches/{bid}", f"/batches/{bid}/versions/1"):
            url = f"http://127.0.0.1:{self.port}{path}"
            with urllib.request.urlopen(url) as resp:
                html = resp.read().decode()
            self.assertIn("跨文物共因风险组", html)


class PackageApiTest(unittest.TestCase):
    """离线采集包上传：合并、冲突检查、幂等、并发、校验。"""

    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test-pkg.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _setup_batch(self):
        """建批次 + 节点(库房/运输车) + 一件文物，返回 (bid, aid, n1, n2)。"""
        st, batch = self.req("POST", "/api/batches", {"name": "离线采集包测试"})
        self.assertEqual(st, 201)
        bid = batch["id"]
        for seq, name in [(1, "库房"), (2, "运输车")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "青花瓷瓶", "temp_min": 18, "temp_max": 24,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 201)
        _, det = self.req("GET", f"/api/batches/{bid}")
        n1, n2 = [n["id"] for n in det["nodes"]]
        return bid, art["id"], n1, n2

    @staticmethod
    def _r(aid, node, ts, t, h=50):
        return {"artifact_id": aid, "node_id": node, "ts": ts,
                "temperature": t, "humidity": h}

    def _version_count(self, bid):
        _, versions = self.req("GET", f"/api/batches/{bid}/versions")
        return len(versions)

    def test_basic_merge_and_idempotent_retry(self):
        bid, aid, n1, n2 = self._setup_batch()
        pkg = {
            "package_id": "PKG-0001",
            "base_version": 0,  # 采集时尚无版本
            "note": "运输车离线采集回补",
            "readings": [
                self._r(aid, n1, "2026-10-01T08:00:00Z", 20),
                self._r(aid, n2, "2026-10-01T08:10:00Z", 26),  # 温度越界
                self._r(aid, n2, "2026-10-01T08:20:00Z", 21),
            ],
        }
        st, res = self.req("POST", f"/api/batches/{bid}/packages", pkg)
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        self.assertEqual(res["accepted"], 3)
        self.assertEqual(res["package_id"], "PKG-0001")
        self.assertEqual(res["base_version"], 0)
        self.assertFalse(res["deduplicated"])
        self.assertEqual(len([e for e in res["events"]
                              if e["metric"] == "temperature"]), 1)

        # 相同内容重试 → 返回首次结果，不产生新版本
        st, res2 = self.req("POST", f"/api/batches/{bid}/packages", pkg)
        self.assertEqual(st, 200)
        self.assertTrue(res2["deduplicated"])
        self.assertEqual(res2["version"], 1)
        self.assertEqual(res2["readings"], res["readings"])
        self.assertEqual(self._version_count(bid), 1)

        # 语义相同（读数顺序不同、时间戳写法不同、数值写法不同）也视为同一包
        pkg_equiv = dict(pkg, readings=[
            dict(self._r(aid, n2, "2026-10-01T08:20:00+00:00", 21.0, 50.0)),
            self._r(aid, n2, "2026-10-01T08:10:00Z", 26),
            self._r(aid, n1, "2026-10-01T08:00:00Z", 20),
        ])
        st, res3 = self.req("POST", f"/api/batches/{bid}/packages", pkg_equiv)
        self.assertEqual(st, 200)
        self.assertTrue(res3["deduplicated"])
        self.assertEqual(self._version_count(bid), 1)

        # 同编号但内容不同（读数不同 / 备注不同 / 基准版本不同）→ 拒绝
        for altered in (
            dict(pkg, readings=[self._r(aid, n1, "2026-10-01T09:00:00Z", 20)]),
            dict(pkg, note="改动过的备注"),
            dict(pkg, base_version=1),
        ):
            st, err = self.req("POST", f"/api/batches/{bid}/packages", altered)
            self.assertEqual(st, 409)
            self.assertIn("拒绝重复提交", err["error"])
        self.assertEqual(self._version_count(bid), 1)

        # 第二包：基准 v1（即当前版），正常合并 → 只生成一个新版本 v2
        pkg2 = {
            "package_id": "PKG-0002",
            "base_version": 1,
            "note": "第二批",
            "readings": [self._r(aid, n2, "2026-10-01T08:30:00Z", 27)],
        }
        st, res4 = self.req("POST", f"/api/batches/{bid}/packages", pkg2)
        self.assertEqual(st, 201)
        self.assertEqual(res4["version"], 2)
        self.assertEqual(self._version_count(bid), 2)

        # 旧版快照可查
        st, v1 = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(st, 200)
        self.assertEqual(len(v1["readings"]), 3)

        # 已接收包列表
        st, packages = self.req("GET", f"/api/batches/{bid}/packages")
        self.assertEqual(st, 200)
        self.assertEqual([(p["package_id"], p["base_version"], p["version_no"])
                          for p in packages],
                         [("PKG-0001", 0, 1), ("PKG-0002", 1, 2)])

    def test_conflict_check_against_advanced_version(self):
        bid, aid, n1, n2 = self._setup_batch()
        # v1：两条读数
        st, res = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            self._r(aid, n1, "2026-10-01T08:00:00Z", 26),
            self._r(aid, n1, "2026-10-01T08:10:00Z", 20),
        ]})
        self.assertEqual(st, 201)
        # v2：更正 08:00 的温度 26 → 22
        st, res = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            self._r(aid, n2, "2026-10-01T08:00:00Z", 22),
        ]})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 2)

        # 包内键 (aid, 08:00) 在基准 v1（节点n1/26℃）与当前 v2（节点n2/22℃）不一致
        # → 409，返回冲突键及两版值，整包不写入、不占用包编号
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-C1",
            "base_version": 1,
            "note": "过期基准的包",
            "readings": [
                self._r(aid, n1, "2026-10-01T08:00:00Z", 25),
                self._r(aid, n1, "2026-10-01T08:10:00Z", 20),
            ],
        })
        self.assertEqual(st, 409)
        self.assertIn("整包未写入", err["error"])
        self.assertEqual(err["base_version"], 1)
        self.assertEqual(err["current_version"], 2)
        conflicts = {(c["artifact_id"], c["ts"]): c for c in err["conflicts"]}
        self.assertEqual(set(conflicts), {(aid, "2026-10-01T08:00:00+00:00")})
        c = conflicts[(aid, "2026-10-01T08:00:00+00:00")]
        self.assertEqual(c["base"], {"node_id": n1, "temperature": 26, "humidity": 50})
        self.assertEqual(c["current"], {"node_id": n2, "temperature": 22, "humidity": 50})
        self.assertEqual(self._version_count(bid), 2)
        st, packages = self.req("GET", f"/api/batches/{bid}/packages")
        self.assertEqual(packages, [])  # 冲突包不记录，修正后可用同编号重传

        # 基准 v0（尚无版本）但当前版已存在该键 → 同样冲突（base 侧为 null）
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-C2",
            "base_version": 0,
            "readings": [self._r(aid, n1, "2026-10-01T08:10:00Z", 20)],
        })
        self.assertEqual(st, 409)
        self.assertIsNone(err["conflicts"][0]["base"])
        self.assertEqual(err["conflicts"][0]["current"]["temperature"], 20)

        # 包内键在两版间一致（08:10 未被改动）+ 新键 → 合并成功，只生成一个版本
        st, res = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-C1",  # 冲突未占用编号，可复用
            "base_version": 1,
            "note": "修正后的包",
            "readings": [
                self._r(aid, n1, "2026-10-01T08:10:00Z", 21),  # 更正两版一致的键
                self._r(aid, n2, "2026-10-01T08:20:00Z", 23),  # 新键
            ],
        })
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 3)
        self.assertEqual(res["corrected"], 1)
        self.assertEqual(self._version_count(bid), 3)

        # 基准等于当前版：不做冲突检查，沿用现有校验（相同读数 → 重复时间戳）
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-C3",
            "base_version": 3,
            "readings": [self._r(aid, n1, "2026-10-01T08:10:00Z", 21)],
        })
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))
        self.assertEqual(self._version_count(bid), 3)

    def test_invalid_base_version_and_payload(self):
        bid, aid, n1, n2 = self._setup_batch()
        st, res = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-V1", "base_version": 0,
            "readings": [self._r(aid, n1, "2026-10-01T08:00:00Z", 20)],
        })
        self.assertEqual(st, 201)

        reading = self._r(aid, n1, "2026-10-01T09:00:00Z", 20)
        # 无效基准版本：不存在 / 负数 / 非整数 / 缺失
        for bad_base in (99, -1, "abc", None, 1.5):
            st, err = self.req("POST", f"/api/batches/{bid}/packages", {
                "package_id": f"PKG-BAD-{bad_base}",
                "base_version": bad_base,
                "readings": [reading],
            })
            self.assertEqual(st, 400, bad_base)
            self.assertIn("无效基准版本", err["error"])
        # 缺包编号 / 空包编号
        for bad_pid in (None, "", "   "):
            st, err = self.req("POST", f"/api/batches/{bid}/packages", {
                "package_id": bad_pid, "base_version": 1, "readings": [reading],
            })
            self.assertEqual(st, 400)
            self.assertIn("包编号不能为空", err["error"])
        # 跨批次文物 / 节点
        bid2, aid2, _, _ = self._setup_batch()
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X1", "base_version": 1,
            "readings": [self._r(aid2, n1, "2026-10-01T09:00:00Z", 20)],
        })
        self.assertEqual(st, 400)
        self.assertIn("不属于批次", json.dumps(err, ensure_ascii=False))
        _, det2 = self.req("GET", f"/api/batches/{bid2}")
        other_node = det2["nodes"][0]["id"]
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X2", "base_version": 1,
            "readings": [self._r(aid, other_node, "2026-10-01T09:00:00Z", 20)],
        })
        self.assertEqual(st, 400)
        self.assertIn("不属于批次", json.dumps(err, ensure_ascii=False))
        # 非法读数：非数值 / 非法时间戳 / 空列表 / 提交内重复
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X3", "base_version": 1,
            "readings": [self._r(aid, n1, "2026-10-01T09:00:00Z", "偏高")],
        })
        self.assertEqual(st, 400)
        self.assertIn("非数值", json.dumps(err, ensure_ascii=False))
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X4", "base_version": 1,
            "readings": [self._r(aid, n1, "不是时间", 20)],
        })
        self.assertEqual(st, 400)
        self.assertIn("时间戳格式无效", json.dumps(err, ensure_ascii=False))
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X5", "base_version": 1, "readings": [],
        })
        self.assertEqual(st, 400)
        self.assertIn("读数列表不能为空", err["error"])
        st, err = self.req("POST", f"/api/batches/{bid}/packages", {
            "package_id": "PKG-X6", "base_version": 1,
            "readings": [reading, dict(reading, temperature=21)],
        })
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 以上全部不产生新版本、不记录包
        self.assertEqual(self._version_count(bid), 1)
        st, packages = self.req("GET", f"/api/batches/{bid}/packages")
        self.assertEqual([p["package_id"] for p in packages], ["PKG-V1"])
        # 批次不存在 → 404
        st, err = self.req("POST", "/api/batches/9999/packages", {
            "package_id": "PKG-X", "base_version": 0, "readings": [reading],
        })
        self.assertEqual(st, 404)

    def test_concurrent_uploads(self):
        bid, aid, n1, n2 = self._setup_batch()
        pkg = {
            "package_id": "PKG-CONC",
            "base_version": 0,
            "note": "并发重试",
            "readings": [self._r(aid, n1, "2026-10-01T08:00:00Z", 20)],
        }

        def upload(payload, results):
            results.append(self.req("POST", f"/api/batches/{bid}/packages", payload))

        # 并发上传同编号同内容 → 恰好一次 201，其余 200，只生成一个版本
        results = []
        threads = [threading.Thread(target=upload, args=(pkg, results))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 201])
        self.assertTrue(all(r["version"] == 1 for _, r in results))
        self.assertEqual(self._version_count(bid), 1)

        # 并发上传同编号不同内容 → 恰好一个 201，其余 409，仍只生成一个版本
        results2 = []
        threads = [
            threading.Thread(target=upload, args=(
                {"package_id": "PKG-CONC2", "base_version": 1, "note": f"内容{i}",
                 "readings": [self._r(aid, n1, "2026-10-01T09:00:00Z", 21)]},
                results2))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses2 = sorted(s for s, _ in results2)
        self.assertEqual(statuses2, [201, 409, 409, 409, 409])
        self.assertEqual(self._version_count(bid), 2)


class HandoverApiTest(unittest.TestCase):
    """运输节点交接：预览核验（通过/待核+原因）、确认冻结、版本/结果变化拒绝。"""

    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test-ho.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _setup_batch(self, artifact_count=2):
        """批次 + 节点(库房/运输车/展厅) + 文物（采样间隔 600 秒）。"""
        st, batch = self.req("POST", "/api/batches", {"name": "交接测试批次"})
        self.assertEqual(st, 201)
        bid = batch["id"]
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        aids = []
        for i in range(artifact_count):
            st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                               {"name": f"文物{i + 1}", "temp_min": 18, "temp_max": 24,
                                "hum_min": 45, "hum_max": 60, "sample_interval": 600})
            self.assertEqual(st, 201)
            aids.append(art["id"])
        _, det = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in det["nodes"]]
        return bid, aids, n1, n2, n3

    @staticmethod
    def _r(aid, node, ts, t=20, h=50):
        return {"artifact_id": aid, "node_id": node, "ts": ts,
                "temperature": t, "humidity": h}

    def _normal_v1(self, bid, aids, n1, n2):
        """v1：文物1 库房→运输车读数齐全；文物2 同样齐全（均正常值）。"""
        readings = [
            self._r(aids[0], n1, "2026-10-01T08:00:00Z"),
            self._r(aids[0], n1, "2026-10-01T08:10:00Z"),
            self._r(aids[0], n2, "2026-10-01T08:20:00Z"),
            self._r(aids[0], n2, "2026-10-01T08:30:00Z"),
        ]
        if len(aids) > 1:
            readings += [
                self._r(aids[1], n1, "2026-10-01T08:00:00Z"),
                self._r(aids[1], n1, "2026-10-01T08:10:00Z"),
                self._r(aids[1], n2, "2026-10-01T08:20:00Z"),
            ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "v1"})
        self.assertEqual(st, 201)
        return res["version"]

    def _preview(self, bid, from_id, to_id, ts):
        return self.req("POST", f"/api/batches/{bid}/handovers/preview",
                        {"from_node_id": from_id, "to_node_id": to_id,
                         "handover_ts": ts})

    def test_preview_pass_and_confirm_freezes(self):
        bid, aids, n1, n2, n3 = self._setup_batch()
        self._normal_v1(bid, aids, n1, n2)

        # 预览：交接时刻 08:15，两文物前节点末条 08:10 / 后节点首条 08:20，均 300 秒
        st, pv = self._preview(bid, n1, n2, "2026-10-01T08:15:00Z")
        self.assertEqual(st, 200)
        self.assertEqual(pv["version"], 1)
        self.assertEqual(pv["result"], "pass")
        self.assertEqual(pv["review_count"], 0)
        self.assertTrue(pv["check_hash"])
        self.assertEqual(pv["from_node"]["name"], "库房")
        self.assertEqual(pv["to_node"]["name"], "运输车")
        self.assertEqual(len(pv["items"]), 2)
        item = pv["items"][0]
        self.assertEqual(item["result"], "pass")
        self.assertEqual(item["reasons"], [])
        self.assertEqual(item["prev_reading"]["ts"], "2026-10-01T08:10:00+00:00")
        self.assertEqual(item["next_reading"]["ts"], "2026-10-01T08:20:00+00:00")

        # 全部通过：无需处置说明即可确认
        st, rec = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z",
            "version": pv["version"], "check_hash": pv["check_hash"],
        })
        self.assertEqual(st, 201)
        self.assertEqual(rec["result"], "pass")
        self.assertEqual(rec["version"], 1)
        self.assertEqual(len(rec["items"]), 2)

        # 后续补录（生成 v2 并产生新事件）、关闭事件：记录均不改写
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            self._r(aids[0], n3, "2026-10-01T09:00:00Z", 30)]})
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        ev = [e for e in res2["events"] if e["status"] == "open"][0]
        st, _ = self.req("POST", f"/api/events/{ev['id']}/close",
                         {"note": "已处置", "evidence_no": "EV-1"})
        self.assertEqual(st, 200)

        st, records = self.req("GET", f"/api/batches/{bid}/handovers")
        self.assertEqual(st, 200)
        self.assertEqual(len(records), 1)
        frozen = records[0]
        self.assertEqual(frozen["version"], 1)  # 冻结在 v1
        self.assertEqual(frozen["result"], "pass")
        self.assertEqual(frozen["items"], rec["items"])
        st, one = self.req("GET", f"/api/batches/{bid}/handovers/{rec['id']}")
        self.assertEqual(st, 200)
        self.assertEqual(one["items"], rec["items"])

        # 旧预览（v1）再确认：当前版本已前进 → 409 要求重核
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z",
            "version": 1, "check_hash": pv["check_hash"],
        })
        self.assertEqual(st, 409)
        self.assertIn("评估版本已变化", err["error"])
        self.assertEqual(err["current_version"], 2)

    def test_preview_review_reasons(self):
        bid, aids, n1, n2, n3 = self._setup_batch()
        self._normal_v1(bid, aids, n1, n2)

        # 后节点在交接时刻后无读数（展厅无任何读数）
        st, pv = self._preview(bid, n2, n3, "2026-10-01T08:25:00Z")
        self.assertEqual(st, 200)
        self.assertEqual(pv["result"], "review")
        self.assertEqual(pv["review_count"], 2)
        for item in pv["items"]:
            self.assertEqual(item["result"], "review")
            self.assertIsNone(item["next_reading"])
            self.assertTrue(any("后节点" in r and "无读数" in r
                                for r in item["reasons"]))

        # 前节点末条读数距交接时刻超过采样间隔（08:10 → 08:25 为 900 秒 > 600）
        st, pv2 = self._preview(bid, n1, n2, "2026-10-01T08:25:00Z")
        item = pv2["items"][0]
        self.assertEqual(item["result"], "review")
        self.assertTrue(any("超过约定采样间隔" in r for r in item["reasons"]))

        # 有待核项：确认必须填写处置说明与证据编号
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n2, "to_node_id": n3,
            "handover_ts": "2026-10-01T08:25:00Z",
            "version": pv["version"], "check_hash": pv["check_hash"],
        })
        self.assertEqual(st, 400)
        self.assertIn("处置说明及证据编号", err["error"])
        st, rec = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n2, "to_node_id": n3,
            "handover_ts": "2026-10-01T08:25:00Z",
            "version": pv["version"], "check_hash": pv["check_hash"],
            "note": "展厅设备未回传，已现场手工记录并补录计划",
            "evidence_no": "EV-HO-0001",
        })
        self.assertEqual(st, 201)
        self.assertEqual(rec["result"], "review")
        self.assertEqual(rec["review_count"], 2)
        self.assertEqual(rec["note"], "展厅设备未回传，已现场手工记录并补录计划")
        self.assertEqual(rec["evidence_no"], "EV-HO-0001")

    def test_preview_gap_crossing(self):
        bid, aids, n1, n2, n3 = self._setup_batch(artifact_count=1)
        aid = aids[0]
        # 08:10 → 08:30 间隔 1200 秒 > 600 → 缺测区间
        st, res = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            self._r(aid, n1, "2026-10-01T08:10:00Z"),
            self._r(aid, n2, "2026-10-01T08:30:00Z"),
        ]})
        self.assertEqual(st, 201)
        self.assertEqual(len(res["gaps"]), 1)

        # 交接 08:20：两读数各距 600 秒（不超），但两读数间跨缺测区间
        st, pv = self._preview(bid, n1, n2, "2026-10-01T08:20:00Z")
        self.assertEqual(st, 200)
        item = pv["items"][0]
        self.assertEqual(item["result"], "review")
        self.assertTrue(any("跨越缺测区间" in r for r in item["reasons"]))
        self.assertEqual(len(item["gaps"]), 1)
        self.assertEqual(item["gaps"][0]["start_ts"], "2026-10-01T08:10:00+00:00")

    def test_open_event_review_and_hash_change_on_close(self):
        bid, aids, n1, n2, n3 = self._setup_batch(artifact_count=1)
        aid = aids[0]
        # 温度 08:00/08:10 越界 → 开放事件 [08:00, 08:10]
        st, res = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            self._r(aid, n1, "2026-10-01T08:00:00Z", 26),
            self._r(aid, n1, "2026-10-01T08:10:00Z", 26),
            self._r(aid, n2, "2026-10-01T08:15:00Z", 20),
            self._r(aid, n2, "2026-10-01T08:25:00Z", 20),
        ]})
        self.assertEqual(st, 201)
        ev = [e for e in res["events"] if e["metric"] == "temperature"][0]

        # 交接时刻 08:10 处于未关闭事件起止区间内 → 待核
        st, pv = self._preview(bid, n1, n2, "2026-10-01T08:10:00Z")
        self.assertEqual(st, 200)
        item = pv["items"][0]
        self.assertEqual(item["result"], "review")
        self.assertTrue(any("未关闭风险事件" in r for r in item["reasons"]))
        self.assertEqual([e["id"] for e in item["events"]], [ev["id"]])

        # 关闭事件（版本号不变）→ 核验结果变化：旧指纹确认被拒（409）
        st, _ = self.req("POST", f"/api/events/{ev['id']}/close",
                         {"note": "已处置", "evidence_no": "EV-2"})
        self.assertEqual(st, 200)
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:10:00Z",
            "version": pv["version"], "check_hash": pv["check_hash"],
        })
        self.assertEqual(st, 409)
        self.assertIn("核验结果已变化", err["error"])

        # 重新预览：事件已关闭 → 通过；确认成功且无需处置说明
        st, pv2 = self._preview(bid, n1, n2, "2026-10-01T08:10:00Z")
        self.assertEqual(pv2["result"], "pass")
        self.assertNotEqual(pv2["check_hash"], pv["check_hash"])
        st, rec = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:10:00Z",
            "version": pv2["version"], "check_hash": pv2["check_hash"],
        })
        self.assertEqual(st, 201)
        self.assertEqual(rec["result"], "pass")

    def test_invalid_inputs_no_record(self):
        bid, aids, n1, n2, n3 = self._setup_batch()
        # 无评估版本：预览与确认均拒绝，不产生记录
        st, err = self._preview(bid, n1, n2, "2026-10-01T08:15:00Z")
        self.assertEqual(st, 400)
        self.assertIn("尚无评估版本", err["error"])
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z", "version": 1, "check_hash": "x",
        })
        self.assertEqual(st, 400)
        self.assertIn("尚无评估版本", err["error"])

        self._normal_v1(bid, aids, n1, n2)
        # 非相邻节点（跨序 / 反向 / 相同）
        for from_id, to_id in [(n1, n3), (n2, n1), (n1, n1)]:
            st, err = self._preview(bid, from_id, to_id, "2026-10-01T08:15:00Z")
            self.assertEqual(st, 400, (from_id, to_id))
        st, err = self._preview(bid, n1, n3, "2026-10-01T08:15:00Z")
        self.assertIn("不是相邻的前后节点", err["error"])
        # 无效交接时刻
        st, err = self._preview(bid, n1, n2, "不是时间")
        self.assertEqual(st, 400)
        self.assertIn("交接时刻格式无效", err["error"])
        # 确认缺核验指纹 / 版本号无效
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z", "version": 1,
        })
        self.assertEqual(st, 400)
        self.assertIn("check_hash", err["error"])
        st, err = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z", "version": "abc",
            "check_hash": "x",
        })
        self.assertEqual(st, 400)
        self.assertIn("预览版本号无效", err["error"])
        # 批次不存在 → 404
        st, err = self._preview(9999, n1, n2, "2026-10-01T08:15:00Z")
        self.assertEqual(st, 404)
        st, err = self.req("GET", "/api/batches/9999/handovers")
        self.assertEqual(st, 404)

        # 以上全部不产生交接记录
        st, records = self.req("GET", f"/api/batches/{bid}/handovers")
        self.assertEqual(st, 200)
        self.assertEqual(records, [])

    def test_pages(self):
        bid, aids, n1, n2, n3 = self._setup_batch()
        self._normal_v1(bid, aids, n1, n2)
        st, pv = self._preview(bid, n1, n2, "2026-10-01T08:15:00Z")
        self.assertEqual(st, 200)
        st, rec = self.req("POST", f"/api/batches/{bid}/handovers", {
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15:00Z",
            "version": pv["version"], "check_hash": pv["check_hash"],
        })
        self.assertEqual(st, 201)

        # 批次页展示交接记录
        url = f"http://127.0.0.1:{self.port}/batches/{bid}"
        with urllib.request.urlopen(url) as resp:
            html = resp.read().decode()
        self.assertIn("运输节点交接", html)
        self.assertIn("交接记录", html)
        self.assertIn("通过", html)

        # 页面表单流程：预览页 → 确认 → 重定向回批次页
        form = urllib.parse.urlencode({
            "from_node_id": n1, "to_node_id": n2,
            "handover_ts": "2026-10-01T08:15",
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/batches/{bid}/handovers/preview",
            data=form, method="POST")
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        self.assertIn("交接预览", html)
        self.assertIn("确认交接", html)


if __name__ == "__main__":
    unittest.main()
