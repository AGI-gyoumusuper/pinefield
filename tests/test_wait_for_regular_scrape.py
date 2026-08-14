import unittest

from wait_for_regular_scrape import workflow_for_account


class WorkflowRoutingTests(unittest.TestCase):
    def test_accounts_one_through_five_use_shared_workflow(self):
        self.assertEqual("scrape.yml", workflow_for_account("account1"))
        self.assertEqual("scrape.yml", workflow_for_account("account5"))

    def test_accounts_six_through_twenty_use_dedicated_workflows(self):
        self.assertEqual("scrape6.yml", workflow_for_account("account6"))
        self.assertEqual("scrape20.yml", workflow_for_account("account20"))

    def test_unknown_account_is_rejected(self):
        with self.assertRaises(ValueError):
            workflow_for_account("account21")


if __name__ == "__main__":
    unittest.main()
