import logging
import logging.handlers

from blockchain.witnet_database import WitnetDatabase

from transactions.data_request import DataRequest
from transactions.commit import Commit
from transactions.reveal import Reveal
from transactions.tally import Tally

class DataRequestReport(object):
    def __init__(self, transaction_type, transaction_hash, consensus_constants, logger=None, log_queue=None, database=None, database_config=None):
        self.transaction_type = transaction_type
        self.transaction_hash = transaction_hash

        self.consensus_constants = consensus_constants
        self.start_time = consensus_constants.checkpoint_zero_timestamp
        self.epoch_period = consensus_constants.checkpoints_period
        self.collateral_minimum = consensus_constants.collateral_minimum

        # Set up logger
        if logger:
            self.logger = logger
        elif log_queue:
            self.log_queue = log_queue
            self.configure_logging_process(log_queue, "node-manager")
            self.logger = logging.getLogger("node-manager")
        else:
            self.logger = None

        if database:
            self.witnet_database = database
        elif database_config:
            self.witnet_database = WitnetDatabase(database_config, logger=self.logger)
        else:
            self.witnet_database = None

    def configure_logging_process(self, queue, label):
        handler = logging.handlers.QueueHandler(queue)
        root = logging.getLogger(label)
        root.handlers = []
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

    def get_data_request_hash(self):
        # Set the data request hash based on the transaction type
        if self.transaction_type == "data_request_txn":
            data_request_hash = self.transaction_hash
            self.logger.info(f"data_request_txn, get_report({data_request_hash})")
        elif self.transaction_type == "commit_txn":
            self.logger.info(f"commit_txn, get_report({self.transaction_hash})")
            self.commit = Commit(self.consensus_constants, logger=self.logger, database=self.witnet_database)
            data_request_hash = self.commit.get_data_request_hash(self.transaction_hash)
            self.logger.info(f"data_request_txn, get_report({data_request_hash})")
        elif self.transaction_type == "reveal_txn":
            self.logger.info(f"reveal_txn, get_report({self.transaction_hash})")
            self.reveal = Reveal(self.consensus_constants, logger=self.logger, database=self.witnet_database)
            data_request_hash = self.reveal.get_data_request_hash(self.transaction_hash)
            self.logger.info(f"data_request_txn, get_report({data_request_hash})")
        elif self.transaction_type == "tally_txn":
            self.logger.info(f"tally_txn, get_report({self.transaction_hash})")
            self.tally = Tally(self.consensus_constants, logger=self.logger, database=self.witnet_database)
            data_request_hash = self.tally.get_data_request_hash(self.transaction_hash)
            self.logger.info(f"data_request_txn, get_report({data_request_hash})")
        return data_request_hash

    def get_report(self):
        self.data_request_hash = self.get_data_request_hash()

        # If there was an error, return the error message
        if "error" in self.data_request_hash:
            self.logger.error(f"Error when fetching data request hash: {self.data_request_hash['error']}")
            return {
                "type": "data_request_report",
                "error": self.data_request_hash["error"]
            }

        # Get details from data request transaction
        self.get_data_request_details()

        # Get all commit, reveal and tally transactions
        self.get_commit_details()
        self.get_reveal_details()
        self.get_tally_details()

        # Add empty reveals for all commits that did not have a matching reveal
        self.add_missing_reveals()
        # Sort commit, reveals and tally by address
        self.sort_by_address()
        # Mark errors and liars
        self.mark_errors()
        self.mark_liars()

        return {
            "type": "data_request_report",
            "transaction_type": self.transaction_type,
            "data_request_txn": self.data_request,
            "commit_txns": self.commits,
            "reveal_txns": self.reveals,
            "tally_txn": self.tally,
            "status": "found",
        }

    def get_data_request_details(self):
        self.logger.info(f"get_data_request_details({self.data_request_hash})")
        data_request = DataRequest(self.consensus_constants, logger=self.logger, database=self.witnet_database)
        self.data_request = data_request.get_transaction_from_database(self.data_request_hash)

    def get_commit_details(self):
        self.logger.info(f"get_commit_details({self.data_request_hash})")
        commit = Commit(self.consensus_constants, logger=self.logger, database=self.witnet_database)
        self.commits = commit.get_commits_for_data_request(self.data_request_hash)

    def get_reveal_details(self):
        self.logger.info(f"get_reveal_details({self.data_request_hash})")
        reveal = Reveal(self.consensus_constants, logger=self.logger, database=self.witnet_database)
        self.reveals = reveal.get_reveals_for_data_request(self.data_request_hash)

    def get_tally_details(self):
        self.logger.info(f"get_tally_details({self.data_request_hash})")
        tally = Tally(self.consensus_constants, logger=self.logger, database=self.witnet_database)
        self.tally = tally.get_tally_for_data_request(self.data_request_hash)

    def add_missing_reveals(self):
        if self.commits and self.reveals:
            commit_addresses = [commit["txn_address"] for commit in self.commits]
            reveal_addresses = [reveal["txn_address"] for reveal in self.reveals]
            for commit_address in commit_addresses:
                if commit_address not in reveal_addresses:
                    # At least one reveal, assume the missing reveal would have been in the same epoch
                    if len(self.reveals) > 0:
                        missing_epoch = self.reveals[0]["epoch"]
                        missing_time = self.reveals[0]["time"]
                    # No reveals, assume they would have been created the epoch after the commit
                    else:
                        missing_epoch = self.commits[0]["epoch"] + 1
                        missing_time = self.start_time + (missing_epoch + 1) * self.epoch_period

                    self.reveals.append({
                        "block_hash": "",
                        "txn_hash": "",
                        "txn_address": commit_address,
                        "reveal": "No reveal",
                        "success": False,
                        "epoch": missing_epoch,
                        "time": missing_time,
                        "status": "",
                        "error": False,
                        "liar": False,
                    })

    def sort_by_address(self):
        if self.commits:
            self.commits = sorted(self.commits, key=lambda l: l["txn_address"])
        if self.reveals:
            self.reveals = sorted(self.reveals, key=lambda l: l["txn_address"])

    def mark_errors(self):
        if self.reveals:
            for reveal in self.reveals:
                if self.tally and reveal["txn_address"] in self.tally["error_addresses"]:
                    reveal["error"] = True

    def mark_liars(self):
        if self.reveals:
            for reveal in self.reveals:
                if self.tally and reveal["txn_address"] in self.tally["liar_addresses"]:
                    reveal["liar"] = True
