"""Credential-isolating, tool-less local model broker smoke probe."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codex_review import (
    _parse_event_stream,
    _pinned_features,
    _unique_event_object,
)
from .review_contract import CodexReviewError
from .review_runtime import ProcessOutput, execute_attested_codex

NONCE = re.compile(r"[0-9a-f]{32}")
SMOKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nonce", "status"],
    "properties": {
        "nonce": {"type": "string", "minLength": 32, "maxLength": 32},
        "status": {"enum": ["ready"]},
    },
}
Dispatch = Callable[..., ProcessOutput]


@dataclass(frozen=True)
class ModelBrokerResult:
    """Validated identity returned by one fresh broker model session."""

    session_id: str
    nonce: str


class ModelBrokerError(RuntimeError):
    """Raised when the credential-isolating broker fails closed."""


class ModelBrokerSmokeProbe:
    """Prove a fixed model call without repository, worker, or credential mounts."""

    def __init__(
        self,
        *,
        dispatch: Dispatch = execute_attested_codex,
        timeout_seconds: int = 300,
    ) -> None:
        if not callable(dispatch) or not isinstance(timeout_seconds, int):
            raise ModelBrokerError("model broker configuration is invalid")
        if not 30 <= timeout_seconds <= 600:
            raise ModelBrokerError("model broker timeout is invalid")
        self._dispatch = dispatch
        self._timeout_seconds = timeout_seconds

    def run(self, nonce: str) -> ModelBrokerResult:
        """Run one fixed, tool-less authenticated model request."""
        if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
            raise ModelBrokerError("model broker nonce is invalid")
        prompt = (
            "Return the JSON object required by the schema. Copy this controller "
            f"nonce exactly and set status to ready: {nonce}"
        ).encode("ascii")
        try:
            with tempfile.TemporaryDirectory(
                prefix="td-model-broker-", dir="/var/tmp"
            ) as temporary:
                root = Path(temporary)
                schema = root / "smoke-schema.json"
                schema.write_text(json.dumps(SMOKE_SCHEMA), encoding="ascii")
                output = self._dispatch(
                    root / "runtime",
                    self._command(schema),
                    input_bytes=prompt,
                    cwd=root,
                    timeout_seconds=self._timeout_seconds,
                )
            if output.returncode != 0 or output.stderr:
                raise ModelBrokerError("model broker process failed")
            session_id, message = _parse_event_stream(output.stdout)
            payload = json.loads(
                message, object_pairs_hook=_unique_event_object
            )
        except ModelBrokerError:
            raise
        except (CodexReviewError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelBrokerError("model broker response failed validation") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"nonce", "status"}
            or payload.get("nonce") != nonce
            or payload.get("status") != "ready"
        ):
            raise ModelBrokerError("model broker response failed validation")
        return ModelBrokerResult(session_id=session_id, nonce=nonce)

    @staticmethod
    def _command(schema: Path) -> list[str]:
        feature_options = [
            argument
            for feature in _pinned_features()
            for argument in ("--disable", feature)
        ]
        return [
            "-a", "never", "exec",
            "--ignore-user-config", "--ignore-rules", "--strict-config",
            "--skip-git-repo-check", "--ephemeral", "--json",
            "--output-schema", str(schema), *feature_options,
            "-c", 'default_permissions="deny-all"',
            "-c", 'permissions.deny-all.filesystem={":root"="none",":minimal"="read"}',
            "-c", "permissions.deny-all.network.enabled=false",
            "-c", 'shell_environment_policy.inherit="none"',
            "-c", 'shell_environment_policy.set={PATH="/usr/bin:/bin"}',
            "-c", 'web_search="disabled"',
            "-c", "tools.web_search=false",
            "-c", "tools.experimental_request_user_input.enabled=false",
            "-c", "tools.update_plan.enabled=false",
            "-c", 'developer_instructions="Return only the schema JSON. Use no tools."',
            "-c", 'model_reasoning_effort="low"', "-",
        ]


def run_local_probe() -> None:
    result = ModelBrokerSmokeProbe().run("0" * 32)
    if not result.session_id:
        raise ModelBrokerError("model broker returned no session identity")


if __name__ == "__main__":
    run_local_probe()
    print("Tool-less model broker smoke proof passed.")
