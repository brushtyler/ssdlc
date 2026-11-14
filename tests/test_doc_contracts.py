"""Behaviour of the documentation contract helpers (issue #54)."""
import tempfile
import unittest
from pathlib import Path

import doc_contracts as docs


class Repo:
    def __init__(self, files):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def close(self):
        self._tmp.cleanup()


class DocContractTests(unittest.TestCase):
    def repo(self, files):
        repo = Repo(files)
        self.addCleanup(repo.close)
        return repo.root

    def assert_contract_fails(self, corpus, phrases=(), issues=()):
        with self.assertRaises(AssertionError) as raised:
            docs.assert_documented(self, corpus, phrases, issues)
        return str(raised.exception)

    def test_follows_links_recursively_inside_docs(self):
        root = self.repo({
            'README.md': 'See [guide](docs/guide.md#usage) and [site](https://example.com/x.md).',
            'docs/guide.md': 'Hashes are not authentication. Next: [deep](sub/deep.md).',
            'docs/sub/deep.md': 'There is no shipped native probe. Back to [guide](../guide.md).',
        })
        corpus = docs.user_facing_docs(root)
        self.assertEqual(corpus.names, ('README.md', 'docs/guide.md', 'docs/sub/deep.md'))
        docs.assert_documented(self, corpus, ['not authentication', 'no shipped native probe'])

    def test_moving_a_linked_guide_keeps_the_contract(self):
        for layout in ({'README.md': '[a](docs/a.md)', 'docs/a.md': 'not authentication'},
                       {'README.md': '[b](docs/topic/b.md)', 'docs/topic/b.md': 'not authentication'}):
            docs.assert_documented(self, docs.user_facing_docs(self.repo(layout)), ['not authentication'])

    def test_removed_limit_fails_and_names_the_phrase(self):
        root = self.repo({'README.md': '[g](docs/g.md)', 'docs/g.md': 'Hashes are unkeyed.'})
        message = self.assert_contract_fails(docs.user_facing_docs(root), ['not authentication'])
        self.assertIn("'not authentication'", message)
        self.assertIn('docs/g.md', message)

    def test_unlinked_or_out_of_scope_pages_do_not_count(self):
        root = self.repo({
            'README.md': '[plan](PLAN_X.md) [agents](AGENTS.md) [evidence](docs/e.json)',
            'PLAN_X.md': 'not authentication',
            'AGENTS.md': 'not authentication',
            'docs/e.json': '"not authentication"',
            'docs/orphan.md': 'not authentication',
        })
        corpus = docs.user_facing_docs(root)
        self.assertEqual(corpus.names, ('README.md',))
        self.assert_contract_fails(corpus, ['not authentication'])

    def test_missing_link_target_and_escape_are_ignored(self):
        root = self.repo({'README.md': '[gone](docs/missing.md) [up](docs/../../outside.md)'})
        self.assertEqual(docs.user_facing_docs(root).names, ('README.md',))

    def test_normalization_ignores_spacing_case_and_markup(self):
        root = self.repo({'README.md': '[g](docs/g.md)',
                          'docs/g.md': '`start  --ticket`\n**Not\nAuthentication**'})
        docs.assert_documented(self, docs.user_facing_docs(root), ['start --ticket', 'not authentication'])

    def test_issue_references_match_whole_numbers_only(self):
        for text, expected in [('see #1.', True), ('(#1)', True), ('[#1](https://x/1)', True),
                               ('issues #14 and #43', False), ('##1', False), ('a#1', False),
                               ('[x](#1-intro)', False)]:
            with self.subTest(text=text):
                self.assertEqual(docs.mentions_issue(docs.normalize(text), 1), expected)
        root = self.repo({'README.md': 'Tracked in #14 and #43.'})
        message = self.assert_contract_fails(docs.user_facing_docs(root), issues=(1, 4, 14))
        self.assertIn("'#1'", message)
        self.assertIn("'#4'", message)
        self.assertNotIn("'#14'", message)

    def test_plan_section_is_bounded_by_the_next_heading_of_its_level(self):
        root = self.repo({'PLAN.md': '\n'.join([
            '## 1. Intro', 'not authentication',
            '## 2. Status', 'pending work', '### 2.1 Detail', '```bash', '## not a heading', '```',
            'detail text', '## 3. Later', 'out of section'])})
        section = docs.plan_section('PLAN.md', '2. Status', root)
        docs.assert_documented(self, section, ['pending work', 'detail text', '## not a heading'])
        self.assert_contract_fails(section, ['not authentication'])
        self.assert_contract_fails(section, ['out of section'])
        last = docs.plan_section('PLAN.md', '## 3. Later', root)
        docs.assert_documented(self, last, ['out of section'])
        with self.assertRaises(AssertionError):
            docs.plan_section('PLAN.md', '9. Missing', root)

    def test_repository_documentation_is_reachable(self):
        corpus = docs.user_facing_docs()
        self.assertIn('docs/supervision.md', corpus.names)
        self.assertIn('docs/bounce-preview.md', corpus.names)
        self.assertIn('README.md', corpus.names)
        self.assertIn('docs/native-gate-convergence.md', corpus.names)
        self.assertFalse(any(name.startswith('PLAN_') or name == 'AGENTS.md' for name in corpus.names))


if __name__ == '__main__':
    unittest.main()
