"""T17-B1：候选刷新身份合同（纯逻辑，无 UI/网络）。

对应 A 端 T17-B1 验收第 9 节测试清单，并追加拷问场景：
SESSION 与 META 互不干扰；旧 request 与新 region 都要能被丢弃。
"""

import pytest

from app.commands import (
    CandidateKind,
    CandidateRegion,
    CandidateRequest,
    CandidateResult,
    candidate_result_is_stale,
)


def region(kind: CandidateKind, *, directory: str = "/proj", endpoint: str = "http://localhost:8080", project_key: str | None = None) -> CandidateRegion:
    return CandidateRegion(kind=kind, endpoint=endpoint, directory=directory, project_key=project_key)


class TestCandidateDto:
    def test_frozen_confirmed_classname(self):
        import dataclasses
        assert dataclasses.is_dataclass(CandidateRequest) is True
        assert dataclasses.is_dataclass(CandidateResult) is True
        assert dataclasses.is_dataclass(CandidateRegion) is True
        req = CandidateRequest(request_id="a", region=region(CandidateKind.SESSION))
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.request_id = "changed"  # type: ignore[misc]

    def test_empty_request_id_rejected(self):
        with pytest.raises(ValueError):
            CandidateRequest(request_id="", region=region(CandidateKind.SESSION))

    def test_whitespace_request_id_rejected(self):
        with pytest.raises(ValueError):
            CandidateRequest(request_id="   ", region=region(CandidateKind.SESSION))

    def test_values_and_metadata_are_tuples(self):
        result = CandidateResult(
            request_id="r1",
            region=region(CandidateKind.MODEL),
            values=("M-a", "M-b"),
            metadata=(("来源", "Fake"),),
            source="Fake",
        )
        assert result.values == ("M-a", "M-b")
        assert result.metadata == (("来源", "Fake"),)
        assert result.source == "Fake"

    def test_source_and_metadata_preserved(self):
        result = CandidateResult(
            request_id="r1",
            region=region(CandidateKind.AGENT),
            values=("agent-x",),
            metadata=(("用户配置", "agent-x"),),
            source="用户配置",
        )
        got = str(result)
        assert result.source == "用户配置"
        assert result.metadata[0][1] == "agent-x"
        assert got

    def test_empty_values_is_legit_success(self):
        result = CandidateResult(
            request_id="r1",
            region=region(CandidateKind.SESSION),
            values=(),
            source="Fake",
        )
        assert result.values == ()
        assert result.error is None
        assert result.request_id == "r1"

    def test_error_result_keeps_request_identity(self):
        result = CandidateResult(
            request_id="r1",
            region=region(CandidateKind.META),
            error="接口不可达",
        )
        assert result.error == "接口不可达"
        assert result.request_id == "r1"


class TestStaleRules:
    def test_same_request_same_region_current(self):
        pending = CandidateRequest(request_id="a", region=region(CandidateKind.SESSION))
        result = CandidateResult(request_id="a", region=region(CandidateKind.SESSION))
        assert candidate_result_is_stale(pending, result) is False

    def test_same_region_old_request_id_stale(self):
        pending = CandidateRequest(request_id="b", region=region(CandidateKind.SESSION))
        result = CandidateResult(request_id="a", region=region(CandidateKind.SESSION))
        assert candidate_result_is_stale(pending, result) is True

    def test_same_request_id_different_directory_stale(self):
        pending = CandidateRequest(request_id="a", region=region(CandidateKind.SESSION, directory="/old"))
        result = CandidateResult(request_id="a", region=region(CandidateKind.SESSION, directory="/new"))
        assert candidate_result_is_stale(pending, result) is True

    def test_same_request_id_different_endpoint_stale(self):
        pending = CandidateRequest(request_id="a", region=region(CandidateKind.AGENT, endpoint="http://e1"))
        result = CandidateResult(request_id="a", region=region(CandidateKind.AGENT, endpoint="http://e2"))
        assert candidate_result_is_stale(pending, result) is True

    def test_same_request_id_different_project_stale(self):
        pending = CandidateRequest(request_id="a", region=region(CandidateKind.MODEL, project_key="p1"))
        result = CandidateResult(request_id="a", region=region(CandidateKind.MODEL, project_key="p2"))
        assert candidate_result_is_stale(pending, result) is True

    def test_session_and_meta_pending_independent(self):
        session_pending = CandidateRequest(request_id="s1", region=region(CandidateKind.SESSION))
        meta_pending = CandidateRequest(request_id="m1", region=region(CandidateKind.META))
        session_result = CandidateResult(request_id="s1", region=region(CandidateKind.SESSION))
        meta_result = CandidateResult(
            request_id="m1",
            region=region(CandidateKind.META),
            values=(),
            source="Fake",
        )
        # SESSION pending（S1）下，META 结果（M1）不同区域 → 丢弃
        assert candidate_result_is_stale(session_pending, meta_result) is True
        # META pending（M1）下，SESSION 结果（S1）不同区域 → 丢弃
        assert candidate_result_is_stale(meta_pending, session_result) is True

    def test_out_of_order_model_regions(self):
        old = CandidateRequest(request_id="r1", region=region(CandidateKind.MODEL, directory="/old"))
        new = CandidateRequest(request_id="r2", region=region(CandidateKind.MODEL, directory="/new"))
        # 新目录先返回，接受
        assert candidate_result_is_stale(new, CandidateResult(request_id="r2", region=region(CandidateKind.MODEL, directory="/new"))) is False
        # 旧目录后返回，必须 stale 丢弃
        assert candidate_result_is_stale(new, CandidateResult(request_id="r1", region=region(CandidateKind.MODEL, directory="/old"))) is True

    def test_pending_none_is_stale(self):
        assert candidate_result_is_stale(None, CandidateResult(request_id="a", region=region(CandidateKind.AGENT))) is True


class TestInterleavedRealistic:
    def test_session_and_meta_both_accepted(self):
        pending_session = CandidateRequest(request_id="s1", region=region(CandidateKind.SESSION))
        pending_meta = CandidateRequest(request_id="m1", region=region(CandidateKind.META))
        session_result = CandidateResult(request_id="s1", region=region(CandidateKind.SESSION), values=("s-a",))
        meta_result = CandidateResult(request_id="m1", region=region(CandidateKind.META), values=(), source="Fake")
        # M1 先回来 → META 接受
        assert candidate_result_is_stale(pending_session, meta_result) is True
        assert candidate_result_is_stale(pending_session, session_result) is False
        # S1 后回来 → SESSION 仍接受
        assert candidate_result_is_stale(pending_session, session_result) is False
        assert candidate_result_is_stale(pending_meta, meta_result) is False