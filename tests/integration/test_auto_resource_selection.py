"""Integration tests for Automatic Relevance-Based Resource Selection."""
import tempfile
import unittest
from pathlib import Path

from brain.context.context_builder import ContextBuilder
from brain.knowledge.document_registry import DocumentRegistry

#: Knowledge docs for the selection check. The default DocumentRegistry indexes
#: ``<workspace>/docs``, whose content depends on where the suite runs (it used
#: to be a developer's whole workspace, full of Azure runbooks; since f5dab8f it
#: is this repo, which has none). A fixture keeps the relevance assertion
#: meaningful and independent of the checkout.
_FIXTURE_DOCS = {
    "azure-deployment-pipeline.md": (
        "# Azure Deployment Pipeline\n\n"
        "How the application is deployed to Azure through the CI/CD pipeline:\n"
        "build the image, push to the registry, and roll out to AKS.\n"
    ),
    "regex-parsing-notes.md": (
        "# Regex Parsing Notes\n\nTips for writing Python regular expressions.\n"
    ),
}


class TestAutoResourceSelection(unittest.TestCase):

    def setUp(self):
        self._docs_dir = tempfile.TemporaryDirectory()
        root = Path(self._docs_dir.name)
        for name, body in _FIXTURE_DOCS.items():
            (root / name).write_text(body, encoding="utf-8")
        doc_registry = DocumentRegistry(knowledge_roots=[str(root)])
        doc_registry.discover()

        self.builder = ContextBuilder()
        self.builder.selector.document_registry = doc_registry

    def tearDown(self):
        self._docs_dir.cleanup()

    def test_azure_deployment_auto_selection(self):
        ctx = self.builder.build_context("Deploy the application to Azure.")

        # Inferred domain
        self.assertEqual(ctx.domain, "DevOps")

        # Inferred MCP servers
        mcp_ids = [m["id"] for m in ctx.relevant_mcps]
        self.assertIn("azure", mcp_ids)

        # Inferred CLI tools
        cli_names = [c["name"] for c in ctx.cli_tools]
        self.assertIn("az", cli_names)

        # Inferred documentation: the relevant doc is picked, the irrelevant one is not.
        doc_titles = [d["title"].lower() for d in ctx.relevant_docs]
        self.assertTrue(any("pipeline" in t or "azure" in t for t in doc_titles), doc_titles)
        self.assertNotIn("regex parsing notes", doc_titles)

        # Inferred steering
        self.assertGreater(len(ctx.relevant_steering), 0)

    def test_irrelevant_task_does_not_attach_azure(self):
        ctx = self.builder.build_context("Fix regex parsing in python test suite")
        mcp_ids = [m["id"] for m in ctx.relevant_mcps]
        self.assertNotIn("azure", mcp_ids)

        cli_names = [c["name"] for c in ctx.cli_tools]
        self.assertNotIn("az", cli_names)
        self.assertNotIn("terraform", cli_names)


if __name__ == "__main__":
    unittest.main()
