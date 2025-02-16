import optparse
import pylibmc
import sys
import time
import toml

from caching.client import Client

from util.logger import configure_logger

class BalanceList(Client):
    def __init__(self, config):
        # Setup logger
        log_filename = config["api"]["caching"]["scripts"]["balance_list"]["log_file"]
        log_level = config["api"]["caching"]["scripts"]["balance_list"]["level_file"]
        self.logger = configure_logger("balance-list", log_filename, log_level)

        # Read some Witnet node parameters
        self.node_retries = config["api"]["caching"]["node_retries"]
        self.node_timeout = config["api"]["caching"]["scripts"]["balance_list"]["node_timeout"]

        # Initialize self.witnet_node, self.witnet_database and self.memcached_client
        super().__init__(config, node=True, timeout=self.node_timeout, database=True, memcached_client=True)

    def build(self):
        start = time.perf_counter()

        # Fetch labeled addresses
        self.logger.info("Fetching tagged addresses")
        sql = """
            SELECT
                address,
                label
            FROM addresses
            WHERE
                label IS NOT NULL
        """
        address_labels = self.witnet_database.sql_return_all(sql)
        address_labels = {address: label for address,label in address_labels}
        self.logger.info(f"Found {len(address_labels)} tagged addresses")
        self.logger.debug(f"Tagged addresses: {address_labels}")

        # Attempt to fetch all non-zero balances for all addresses in the network
        self.logger.info("Fetching all address balances")
        address_balances = self.witnet_node.get_balance_all()

        # On fail: retry for a configurable amount of times (adding a sleep timeout)
        attempts = 0
        while type(address_balances) is dict and "error" in address_balances:
            self.logger.error(f"Failed to fetch all address balances: {address_balances}")

            address_balances = self.witnet_node.get_balance_all()
            if "result" in address_balances:
                break

            attempts += 1
            if attempts == self.node_retries:
                self.logger.error(f"Maximum retries ({self.node_retries}) to fetch all address balances exceeded")
                return False

            time.sleep(attempts)

        self.addresses, self.balances, self.balances_sum = [], [], 0
        for address, balance in address_balances["result"].items():
            # Only save addresses with a balance above 1 WIT
            if balance["total"] // 1E9 < 1:
                continue
            self.addresses.append(address)
            # Create balance entry
            self.balances.append([address, balance["total"] // 1E9, address_labels[address] if address in address_labels else ""])
            # Sum all balances, don't floor to an integer to minimize rounding errors
            self.balances_sum += balance["total"] / 1E9
        self.balances_sum = int(self.balances_sum)
        # Sort balance list by largest balance first
        self.balances = sorted(self.balances, key=lambda l: l[1], reverse=True)

        self.logger.info(f"Processed {len(self.balances)} address balances in {time.perf_counter() - start:.2f}s")

        return True

    # Save the BalanceList data into a memcached instance
    def save(self, items_per_key=1000):
        self.logger.info("Saving all data in our memcached instance")

        # Save the total balance for all BalanceList entries
        self.memcached_client.set(f"balance-list_sum", self.balances_sum)

        # Save timestamp of when the BalanceList was last updated
        self.memcached_client.set(f"balance-list_updated", int(time.time()))

        # Save the actual BalanceList per x items as to not exceed the maximum item size of 1MB
        items_stored_in_cache = 0
        for i in range(0, len(self.balances), items_per_key):
            self.logger.debug(f"Saving balance-list_{i}-{i + items_per_key}")
            try:
                self.memcached_client.set(f"balance-list_{i}-{i + items_per_key}", self.balances[i : i + items_per_key])
            except pylibmc.TooBig as e:
                self.logger.warning("Could not save BalanceList sublist in cache because the item size exceeded 1MB")
            items_stored_in_cache += 1

        self.memcached_client.set(f"balance-list_items", items_stored_in_cache)

    def get_address_ids(self):
        # Fetch all known addresses and their ids
        sql = """
            SELECT
                address,
                id
            FROM
                addresses
        """
        addresses = self.witnet_database.sql_return_all(sql)

        # Transform list of data to dictionary
        self.address_ids = {}
        if addresses:
            for address, address_id in addresses:
                self.address_ids[address] = address_id

    def insert_addresses(self):
        start = time.perf_counter()

        # Check which addresses we need to insert
        self.get_address_ids()
        addresses_to_insert = []
        for address in self.addresses:
            if address not in self.address_ids:
                addresses_to_insert.append([address])

        if len(addresses_to_insert) > 0:
            # Insert them
            sql = """
                INSERT INTO addresses (
                    address
                ) VALUES %s
            """
            self.witnet_database.sql_execute_many(sql, addresses_to_insert)

        self.logger.info(f"Inserted {len(addresses_to_insert)} addresses into database in {time.perf_counter() - start:.2f}s")

def main():
    parser = optparse.OptionParser()
    parser.add_option("--config-file", type="string", default="explorer.toml", dest="config_file", help="Specify a configuration file")
    options, args = parser.parse_args()

    if options.config_file == None:
        sys.stderr.write("Need to specify a configuration file!\n")
        sys.exit(1)

    # Load config file
    config = toml.load(options.config_file)

    # Create BalanceList cache
    balance_list = BalanceList(config)
    # Save BalanceList in memcached instance on success
    if balance_list.build():
        balance_list.save()
        balance_list.insert_addresses()

if __name__ == "__main__":
    main()
