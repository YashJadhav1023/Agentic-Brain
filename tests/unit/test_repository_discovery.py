"""Unit tests for Git repository discovery."""
import unittest
from pathlib import Path

from brain.resources.repo_registry import RepositoryRegistry


class TestRepositoryDiscovery(unittest.TestCase):

    def setUp(self):
        self.registry = RepositoryRegistry()
        self.repos = self.registry.discover()

    #: The repository's own directory name. Hardcoding "Agentic_shared_memory" made
    #: these tests fail for anyone who cloned into a differently named folder.
    @property
    def repo_name(self) -> str:
        return Path(__file__).resolve().parents[2].name

    def test_current_repo_discovered(self):
        names = [r.name for r in self.repos]
        self.assertIn(self.repo_name, names)

    def test_branch_and_clean_status(self):
        repo = self.registry.get_repo(self.repo_name)
        self.assertIsNotNone(repo)
        self.assertIsInstance(repo.branch, str)
        self.assertIsInstance(repo.clean, bool)

    def test_remotes_contain_no_secrets(self):
        for repo in self.repos:
            for rem in repo.remotes:
                # Remotes should be names ('origin'), not raw credential URLs
                self.assertNotIn("http://", rem)
                self.assertNotIn("https://", rem)
                self.assertNotIn("@", rem)


if __name__ == "__main__":
    unittest.main()
