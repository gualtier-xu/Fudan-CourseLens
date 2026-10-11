from __future__ import annotations

import copy
import time
import unittest

from src.remote.protocol import (
    CONTROL_SCHEMA,
    JOB_SCHEMA,
    PROTOCOL_VERSION,
    RESULT_SCHEMA,
    PROCESS_CANARY_FIXTURE_BYTES,
    PROCESS_CANARY_FIXTURE_RECORDS,
    PROCESS_CANARY_FIXTURE_SHA256,
    PROCESS_CANARY_PIPELINE,
    PROCESS_CANARY_SCHEMA,
    ProtocolError,
    chunk_envelope,
    finalize_job,
    generate_box_keypair,
    generate_signing_keypair,
    join_envelope,
    open_job,
    open_control,
    open_result,
    seal_job,
    seal_control,
    seal_result,
)


TASK_ID = "0123456789abcdef0123456789abcdef"


class RemoteProtocolTests(unittest.TestCase):
    def setUp(self):
        self.worker_private, self.worker_public = generate_box_keypair()
        self.result_private, self.result_public = generate_box_keypair()
        self.signing_private, self.signing_public = generate_signing_keypair()

    def job(self):
        now = time.time()
        return finalize_job({
            "schema": JOB_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "echo",
            "created_at": now,
            "expires_at": now + 300,
            "result_public_key": self.result_public,
            "pipeline": {"version": "test-v1"},
            "payload": {"message": "synthetic only"},
        })

    def test_job_round_trip_and_issue_chunks(self):
        job = self.job()
        envelope = seal_job(job, self.worker_public)
        chunks = chunk_envelope(envelope, chunk_chars=1024)
        rebuilt = join_envelope(chunks)
        self.assertEqual(open_job(rebuilt, self.worker_private)["input_hash"], job["input_hash"])
        self.assertNotIn("synthetic only", "".join(chunks))

    def test_wrong_worker_key_and_tamper_are_rejected(self):
        envelope = seal_job(self.job(), self.worker_public)
        wrong_private, _ = generate_box_keypair()
        with self.assertRaises(ProtocolError):
            open_job(envelope, wrong_private)
        damaged = copy.deepcopy(envelope)
        damaged["sha256"] = "0" * 64
        with self.assertRaises(ProtocolError):
            open_job(damaged, self.worker_private)

    def test_signed_result_binds_task_and_input(self):
        job = self.job()
        result = {
            "schema": RESULT_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "echo",
            "input_hash": job["input_hash"],
            "status": "completed",
            "outputs": {"message": "ok"},
        }
        envelope = seal_result(result, self.result_public, self.signing_private)
        opened = open_result(
            envelope,
            self.result_private,
            self.signing_public,
            expected_task_id=TASK_ID,
            expected_input_hash=job["input_hash"],
        )
        self.assertEqual(opened["outputs"]["message"], "ok")
        with self.assertRaises(ProtocolError):
            open_result(
                envelope,
                self.result_private,
                self.signing_public,
                expected_task_id=TASK_ID,
                expected_input_hash="f" * 64,
            )

    def test_expired_job_is_rejected(self):
        job = self.job()
        envelope = seal_job(job, self.worker_public)
        with self.assertRaises(ProtocolError):
            open_job(envelope, self.worker_private, now=float(job["expires_at"]) + 121)

    def test_media_slice_and_signed_control_round_trip(self):
        job = self.job()
        job["requested_outputs"] = ["subtitle", "summary"]
        job["payload"]["media"] = {"start_seconds": 600, "duration_seconds": 300}
        job = finalize_job(job)
        self.assertEqual(job["payload"]["media"]["start_seconds"], 600)

        control = {
            "schema": CONTROL_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "input_hash": job["input_hash"],
            "control_kind": "checkpoint",
            "sequence": 1,
            "payload": {"stage": "asr", "completed_seconds": 300},
        }
        envelope = seal_control(control, self.result_public, self.signing_private)
        opened = open_control(
            envelope,
            self.result_private,
            self.signing_public,
            expected_task_id=TASK_ID,
            expected_input_hash=job["input_hash"],
        )
        self.assertEqual(opened["payload"]["completed_seconds"], 300)

        damaged = copy.deepcopy(envelope)
        damaged["signature"] = "A" * len(damaged["signature"])
        with self.assertRaises(ProtocolError):
            open_control(
                damaged,
                self.result_private,
                self.signing_public,
                expected_task_id=TASK_ID,
                expected_input_hash=job["input_hash"],
            )

    def test_invalid_media_slice_is_rejected(self):
        job = self.job()
        job["payload"]["media"] = {"start_seconds": -1, "duration_seconds": 300}
        with self.assertRaises(ProtocolError):
            finalize_job(job)

    def test_runner_source_session_is_sealed_and_validated(self):
        job = self.job()
        job["payload"]["media"] = {"start_seconds": 600, "duration_seconds": 300}
        job["payload"]["source_session"] = {
            "provider": "runner-session-v1",
            "course_id": "36941",
            "sub_id": "652577",
            "media": True,
            "slides": False,
        }
        job["secrets"] = {
            "source_credentials": {"account": "synthetic", "password": "synthetic"},
        }
        finalized = finalize_job(job)
        envelope = seal_job(finalized, self.worker_public)
        opened = open_job(envelope, self.worker_private)
        self.assertEqual(opened["payload"]["source_session"]["sub_id"], "652577")
        self.assertNotIn("synthetic", str(envelope))

        invalid = copy.deepcopy(job)
        invalid["secrets"]["source_credentials"]["password"] = ""
        with self.assertRaises(ProtocolError):
            finalize_job(invalid)

    def test_process_canary_job_is_exact_and_rejects_content_fields(self):
        now = time.time()
        base = {
            "schema": JOB_SCHEMA,
            "protocol_version": PROTOCOL_VERSION,
            "task_id": TASK_ID,
            "job_kind": "process_canary",
            "created_at": now,
            "expires_at": now + 300,
            "result_public_key": self.result_public,
            "pipeline": {"version": PROCESS_CANARY_PIPELINE},
            "payload": {},
            "secrets": {},
            "requested_outputs": [],
        }
        finalized = finalize_job(base)
        self.assertEqual(open_job(seal_job(finalized, self.worker_public), self.worker_private), finalized)
        for field, value in (
            ("payload", None),
            ("payload", {"title": "arbitrary text"}),
            ("secrets", {"account": "forbidden"}),
            ("requested_outputs", ["subtitle"]),
            ("pipeline", {"version": "other"}),
        ):
            with self.subTest(field=field, value=value):
                invalid = copy.deepcopy(base)
                invalid[field] = value
                with self.assertRaises(ProtocolError):
                    finalize_job(invalid)

    def test_process_canary_result_is_closed_signed_and_replay_bound(self):
        job = self.process_canary_job_value()
        result = self.process_canary_result(job)
        envelope = seal_result(result, self.result_public, self.signing_private)
        opened = open_result(
            envelope, self.result_private, self.signing_public,
            expected_task_id=TASK_ID, expected_input_hash=job["input_hash"],
        )
        self.assertEqual(opened, result)
        replay_job = self.process_canary_job_value(task_id="f" * 32)
        with self.assertRaises(ProtocolError):
            open_result(
                envelope, self.result_private, self.signing_public,
                expected_task_id=replay_job["task_id"],
                expected_input_hash=replay_job["input_hash"],
            )
        for mutation in ("extra", "profile"):
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(result)
                if mutation == "extra":
                    invalid["outputs"]["process_canary"]["text"] = "forbidden"
                else:
                    invalid["outputs"]["process_canary"]["workflow_profile"] = "echo-v1"
                with self.assertRaises(ProtocolError):
                    seal_result(invalid, self.result_public, self.signing_private)

    def process_canary_job_value(self, *, task_id=TASK_ID):
        now = time.time()
        return finalize_job({
            "schema": JOB_SCHEMA, "protocol_version": PROTOCOL_VERSION,
            "task_id": task_id, "job_kind": "process_canary",
            "created_at": now, "expires_at": now + 300,
            "result_public_key": self.result_public,
            "pipeline": {"version": PROCESS_CANARY_PIPELINE},
            "payload": {}, "secrets": {}, "requested_outputs": [],
        })

    @staticmethod
    def process_canary_result(job):
        return {
            "schema": RESULT_SCHEMA, "protocol_version": PROTOCOL_VERSION,
            "task_id": job["task_id"], "job_kind": "process_canary",
            "input_hash": job["input_hash"],
            "pipeline_fingerprint": PROCESS_CANARY_PIPELINE,
            "status": "completed",
            "outputs": {"process_canary": {
                "schema": PROCESS_CANARY_SCHEMA,
                "fixture_sha256": PROCESS_CANARY_FIXTURE_SHA256,
                "fixture_bytes": PROCESS_CANARY_FIXTURE_BYTES,
                "fixture_records": PROCESS_CANARY_FIXTURE_RECORDS,
                "worker_commit": "a" * 40,
                "workflow_profile": "process-v1",
            }},
            "metrics": {
                "synthetic_bytes": PROCESS_CANARY_FIXTURE_BYTES,
                "synthetic_records": PROCESS_CANARY_FIXTURE_RECORDS,
            },
            "warnings": [],
        }


if __name__ == "__main__":
    unittest.main()
