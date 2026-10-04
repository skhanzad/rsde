"""Expose pytest failures as GitHub annotations and a readable job summary."""

import os
from pathlib import Path
from xml.etree import ElementTree


def main() -> None:
    report = Path(os.environ["RUNNER_TEMP"]) / "rsde-tests.xml"
    if not report.is_file():
        return
    root = ElementTree.parse(report).getroot()
    summary = ["## Test results\n"]
    for suite in root.iter("testsuite"):
        summary.append(
            f"{suite.get('tests')} tests; {suite.get('failures')} failures; "
            f"{suite.get('errors')} errors; {suite.get('skipped')} skipped.\n"
        )
    for case in root.iter("testcase"):
        for failure in [*case.findall("failure"), *case.findall("error")]:
            name = f"{case.get('classname')}.{case.get('name')}"
            detail = (failure.text or failure.get("message", ""))[:16000]
            escaped = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{name}%0A{escaped}")
            summary.append(f"### {name}\n\n```text\n{detail}\n```\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(summary))


if __name__ == "__main__":
    main()
