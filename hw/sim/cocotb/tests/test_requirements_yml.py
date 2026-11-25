# rr unknown-llm (AI-generated)
# SPDX-License-Identifier: MIT

from pathlib import Path

from sdk.cli.requirements import load_requirements


class TestRequirementsYmlLoads:
    def test_all_requirements_yml_files_load_cleanly(self):
        repo_root = Path(__file__).resolve().parents[4]
        requirements_files = sorted(repo_root.glob("hw/rtl/datalogger/*/requirements.yml"))

        assert len(requirements_files) >= 4

        for path in requirements_files:
            load_requirements(str(path))
