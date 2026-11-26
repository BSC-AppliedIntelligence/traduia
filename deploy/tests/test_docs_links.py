"""Documentation links: relative links resolve (file and heading anchor); bundled documents only link to bundled files."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "swarm").glob("**/*.md"))]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")

def slug(heading):
    text = re.sub(r"`", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors(path):
    in_fence, found = False, set()
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,6} ", line):
            found.add(slug(line.lstrip("#")))
    return found


def relative_links(path):
    for target in LINK.findall(path.read_text()):
        if not re.match(r"^(https?:|mailto:)", target):
            yield target


class DocsLinkTests(unittest.TestCase):
    def test_relative_links_resolve_to_files_and_headings(self):
        for doc in DOCS:
            for target in relative_links(doc):
                file_part, _, anchor = target.partition("#")
                resolved = (doc.parent / file_part).resolve() if file_part else doc
                self.assertTrue(resolved.exists(), f"{doc.relative_to(ROOT)} links to missing {target}")
                if anchor and resolved.suffix == ".md":
                    self.assertIn(anchor, anchors(resolved), f"{doc.relative_to(ROOT)} links to missing heading {target}")

    def test_entry_documents_exist_and_cross_link(self):
        quickstart, guide, reference = (ROOT / "docs" / "quickstart.md").read_text(), (ROOT / "docs" / "developer-guide.md").read_text(), (ROOT / "swarm" / "README.md").read_text()
        self.assertIn("developer-guide.md", quickstart)
        self.assertIn("quickstart.md", guide)
        self.assertIn("quickstart.md", reference)
        self.assertIn("docs/quickstart.md", (ROOT / "README.md").read_text())

    def test_quickstart_commands_use_scripts_that_exist(self):
        text = (ROOT / "docs" / "quickstart.md").read_text()
        for script in set(re.findall(r"swarm/(?:scripts|tests/turn-media-test)/[\w.-]+", text)):
            self.assertTrue((ROOT / script).is_file(), script)


if __name__ == "__main__":
    unittest.main()
