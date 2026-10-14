"""Shared fake terraform runner for mcctl tests."""
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "mcctl"


def load(name):
    return json.loads((FIX / name).read_text())


class FakeRunner:
    def __init__(self, show, serial=1, after_apply=None):
        self.show, self.serial, self.after_apply = show, serial, after_apply
        self.calls, self.resources = [], []

    def init(self):
        self.calls.append("init")

    def plan(self, out, destroy=False, targets=(), var_file=None):
        self.calls.append(("plan", destroy, tuple(targets), var_file))
        Path(out).write_bytes(json.dumps(self.show, sort_keys=True).encode())

    def show_json(self, plan):
        return self.show

    def apply(self, plan):
        self.calls.append("apply")
        self.serial += 1
        self.resources = ["aws_s3_bucket.logs"]
        if self.after_apply is not None:
            self.show = self.after_apply

    def state_serial(self):
        return self.serial

    def state_list(self):
        return list(self.resources)

    def outputs(self):
        return {}
