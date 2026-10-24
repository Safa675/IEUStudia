"""Errors in words for anyone (net.plain_error)."""

import errno

import requests

from studia.net import NetworkError, plain_error


def test_errors_are_said_plainly():
    assert plain_error(requests.ConnectionError("Max retries"), "https://huggingface.co/x").startswith(
        "Can't reach huggingface.co. Check your internet connection")
    assert plain_error(requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), "https://learn.ieu.edu.tr/").startswith(
        "A secure connection to learn.ieu.edu.tr was blocked")
    assert plain_error(requests.ReadTimeout("read timed out"), "https://pypi.org/").startswith("pypi.org took too long")
    assert plain_error(OSError(errno.ENOSPC, "No space left on device")).startswith("Your disk is full")
    assert plain_error(MemoryError()).startswith("Not enough memory")
    denied = plain_error(PermissionError(13, "Access is denied", r"C:\Users\x\OneDrive\EEE 215 - Study pack.md"))
    assert denied.startswith("Windows did not let IEUStudia use EEE 215 - Study pack.md") and "Settings" not in denied
    gone = plain_error(FileNotFoundError(3, "The system cannot find the path specified", "Z:\\IEUStudia"))
    assert gone.startswith("Z:\\IEUStudia can't be reached: is the drive")
    assert plain_error(NetworkError("Can't reach panopto.ieu.edu.tr.")) == "Can't reach panopto.ieu.edu.tr."
    assert plain_error(RuntimeError("expected HTTP 206, got 403")) == "expected HTTP 206, got 403."   # one full stop
    assert plain_error(RuntimeError("Panopto is still processing this recording.")).endswith("recording.")
