import json
import os

from util.database_manager import DatabaseManager

class TRS:
    def __init__(self, trs_file_json, load_trs, db_config=None, db_mngr=None, logger=None):
        self.trs_file_json = trs_file_json
        if db_config:
            self.db_mngr = DatabaseManager(db_config, logger=logger)
        else:
            self.db_mngr = db_mngr
        if logger:
            self.logger = logger

        # Reputation-related consensus constants
        self.reputation_issuance_stop = 1 << 20
        self.penalization_factor = 0.5
        self.reputation_expiration = 20000

        # Attempt to load the TRS data from a file if requested
        load_trs_success = load_trs
        if load_trs:
            if self.trs_file_json == "":
                if self.logger:
                    self.logger.warning("No TRS data JSON file supplied, initializing all data to zero")
                load_trs_success = False
            if not os.path.exists(self.trs_file_json):
                if self.logger:
                    self.logger.warning("The supplied TRS data JSON file does not exist, initializing all data to zero")
                load_trs_success = False
            if load_trs_success:
                self.load_trs()
                self.first_update = True

        if not load_trs_success:
            # Total amount of witnessing acts that happened
            self.witnessing_acts = 0
            # Reputation from the previous epoch
            self.leftover_reputation = 0
            # List of reputation gains by epoch
            self.reputation_expiry = []
            # Track the last epoch
            self.epoch = 0

            # Map of identities to reputation
            self.identities = {}

            self.first_update = False

        # Variable for database insertions
        self.insert_reputation_differences = []

        # Get an initial id to address mapping
        self.ids_to_addresses = {}
        self.get_ids_to_addresses()

        # Statistics
        self.max_reputation_distributed = 0
        self.max_reputation_slashed = 0

    #####################################################
    #       Functions to load and persist the TRS       #
    #####################################################

    def persist_trs(self):
        if self.trs_file_json == "":
            if self.logger:
                self.logger.error("Could not persist TRS as no file name was specified")
            return

        if not os.path.exists(os.path.dirname(self.trs_file_json)):
            os.makedirs(os.path.dirname(self.trs_file_json))

        f = open(self.trs_file_json, "w+")
        data = {
            "witnessing_acts": self.witnessing_acts,
            "leftover_reputation": self.leftover_reputation,
            "reputation_expiry": self.reputation_expiry,
            "epoch": self.epoch,
            "identities": self.identities,
        }
        json.dump(data, f)
        f.close()

    def load_trs(self):
        f = open(self.trs_file_json, "r")
        data = json.load(f)
        f.close()

        self.witnessing_acts = data["witnessing_acts"]
        self.leftover_reputation = data["leftover_reputation"]
        self.reputation_expiry = data["reputation_expiry"]
        self.epoch = data["epoch"]
        self.identities = data["identities"]

    #####################################################
    #                Database functions                 #
    #####################################################

    def insert_reputation_difference(self, address, epoch, reputation, reputation_type):
        if self.logger:
            self.logger.debug(f"Inserting {reputation} reputation difference ({reputation_type}) for address {address} at epoch {epoch}")
        self.insert_reputation_differences.append([address, epoch, reputation, reputation_type])

    def finalize_reputation_insertions(self):
        if len(self.insert_reputation_differences) > 0:
            sql = """
                INSERT INTO reputation (
                    address,
                    epoch,
                    reputation,
                    type
                ) VALUES %s
            """
            self.db_mngr.sql_execute_many(sql, self.insert_reputation_differences)
            if self.logger:
                self.logger.debug(f"Inserted {len(self.insert_reputation_differences)} reputation differences")
        self.insert_reputation_differences = []

    def get_addresses_to_ids(self):
        sql = """
            SELECT
                address,
                id
            FROM
                addresses
        """
        addresses = self.db_mngr.sql_return_all(sql)

        # Transform list of data to dictionary
        address_ids = {}
        if addresses:
            for address, address_id in addresses:
                address_ids[address] = address_id

        # Also update the class-local dictionary
        self.address_ids = address_ids

        return address_ids

    def get_ids_to_addresses(self):
        sql = """
            SELECT
                address,
                id
            FROM
                addresses
        """
        addresses = self.db_mngr.sql_return_all(sql)

        # Transform list of data to dictionary
        if addresses:
            self.ids_to_addresses = {}
            for address, address_id in addresses:
                self.ids_to_addresses[address_id] = address

    def insert_addresses(self, addresses_to_insert):
        sql = """
            INSERT INTO addresses (
                address
            ) VALUES %s
        """
        self.db_mngr.sql_execute_many(sql, addresses_to_insert)
        if self.logger:
            self.logger.debug(f"Inserted {len(addresses_to_insert)} addresses")

    def insert_trs(self, next_epoch=False):
        epoch = self.epoch + 1 if next_epoch else self.epoch
        sql = """
            INSERT INTO trs (
                epoch,
                addresses,
                reputations
            ) VALUES (%s, %s, %s)
        """
        addresses, reputations = self.transform_identities()
        self.db_mngr.sql_insert_one(sql, [epoch, addresses, reputations])
        if self.logger:
            self.logger.debug(f"Inserted the TRS for epoch {epoch}")

    def get_trs(self, epoch):
        data = None
        while not data:
            sql = """
                SELECT
                    epoch,
                    addresses,
                    reputations
                FROM
                    trs
                WHERE epoch=%s
            """ % epoch

            # Fetch the data
            data = self.db_mngr.sql_return_one(sql)

            # If we did not find a TRS, decrement the epoch
            epoch -= 1

            if epoch <= 0:
                break

        if data:
            # Create TRS dictionary
            epoch, addresses, reputations = data

            # Optimistic implementation
            # We assume the initial mapping contains all required ids, but if it does not, refresh the mapping
            if set(addresses) - set(self.ids_to_addresses.keys()) != set():
                if self.logger:
                    self.logger.warning("Not all IDs where found in the id to address mapping")
                self.get_addresses_to_ids()

            # Transform to dictionary and calculate total reputation
            identities = {self.ids_to_addresses[address]: reputation for address, reputation in zip(addresses, reputations)}
            total_reputation = sum(identities.values())

            # Calculate eligibilities and create the TRS list
            eligibilities = self.calculate_eligibilities(identities)
            trs = [(address, reputation, eligibilities[address] * 100) for address, reputation in sorted(identities.items(), key=lambda l: l[1], reverse=True)]
        else:
            epoch = 0
            trs = []
            total_reputation = 0

        return epoch, trs, total_reputation

    #####################################################
    #         Reputation manipulation functions         #
    #####################################################

    def filter_honest_identities(self, honests, errors, liars):
        # Identities which reveal multiple times during one epoch only receive one slice of reputation
        # Hence we don't need to track the amount of honest reveals, only their presence
        honest_identities = set()
        for identity, truths in honests.most_common():
            if liars.get(identity, 0) == 0 and truths >= errors.get(identity, 0):
                honest_identities.add(identity)
        return honest_identities, liars

    def issue_reputation(self, new_witnessing_acts):
        if self.witnessing_acts >= self.reputation_issuance_stop:
            # Don't create new reputation
            return 0
        else:
            # Create new reputation up to the total amount the system is allowed to create
            new = min(self.reputation_issuance_stop, self.witnessing_acts + new_witnessing_acts)
            return new - self.witnessing_acts

    def expire_reputation(self, next_epoch=False):
        epoch = self.epoch + 1 if next_epoch else self.epoch

        old_trs = {identity: reputation for identity, reputation in self.identities.items()}

        counter, expired = 0, 0
        while counter < len(self.reputation_expiry):
            # Break out of the loop if the oldest reputation should not expire yet
            if self.reputation_expiry[counter][0] > self.witnessing_acts:
                break
            # Expire the old reputation
            if self.reputation_expiry[counter][0] <= self.witnessing_acts:
                # Update reputation of identities whose reputation is expiring
                for identity, reputation in self.reputation_expiry[counter][1].items():
                    # Count total expired reputation
                    expired += reputation
                    # Update reputation
                    self.identities[identity] -= reputation
                    assert self.identities[identity] >= 0
                    # Insert into database
                    self.insert_reputation_difference(identity, epoch, -reputation, "expire")
                del self.reputation_expiry[counter]
            else:
                counter += 1

        # Log how much reputation expired in total
        for identity in self.identities.keys():
            if self.identities[identity] != old_trs[identity]:
                if self.logger:
                    self.logger.debug(f"{epoch} -- {old_trs[identity] - self.identities[identity]} reputation expired for {identity}")

        return expired

    def expire_reputation_in_next_epoch(self):
        if self.logger:
            self.logger.debug(f"Expiring reputation in next epoch")
        expired_reputation = self.expire_reputation(next_epoch=True)
        if expired_reputation > 0:
            total_reputation = self.leftover_reputation + expired_reputation
            if self.logger:
                self.logger.debug(f"{self.epoch + 1} -- {self.leftover_reputation} from previous epoch + {expired_reputation} expired + 0 issued + 0 penalized = {total_reputation}")
            self.leftover_reputation += expired_reputation

    def penalize_liars(self, liar_identities):
        total_reputation_penalized = 0

        for liar_identity, lies in liar_identities.most_common():
            if liar_identity in self.identities:
                # Calculate leftover reputation
                reputation_after_lies = int(self.identities[liar_identity] * (self.penalization_factor ** lies))
                penalized_reputation = self.identities[liar_identity] - reputation_after_lies

                # Expire reputation packets backwards (removing most recently earned reputation)
                reputation_to_expire = penalized_reputation
                for expiry in reversed(self.reputation_expiry):
                    if liar_identity in expiry[1]:
                        if expiry[1][liar_identity] <= reputation_to_expire:
                            reputation_to_expire -= expiry[1][liar_identity]
                            del expiry[1][liar_identity]
                        else:
                            expiry[1][liar_identity] -= reputation_to_expire
                            reputation_to_expire = 0
                        if reputation_to_expire == 0:
                            break
                assert reputation_to_expire == 0, "Not enough reputation packets found to expire"

                # Track total penalized reputation
                total_reputation_penalized += penalized_reputation

                # Update identity reputation
                self.identities[liar_identity] = reputation_after_lies
                if self.logger:
                    self.logger.debug(f"{self.epoch} -- The reputation score of {liar_identity} has been slashed by {penalized_reputation} points")

                # Insert into database
                self.insert_reputation_difference(liar_identity, self.epoch, -penalized_reputation, "lie")

                # Track statistic
                if penalized_reputation > self.max_reputation_slashed:
                    self.max_reputation_slashed = penalized_reputation

        return total_reputation_penalized

    def distribute_reputation(self, total_reputation, honest_identities):
        reputation_to_distribute = int(total_reputation / (len(honest_identities) or 1))

        if reputation_to_distribute == 0:
            return 0, []

        reputation_earning_identities = []
        for honest_identity in honest_identities:
            # Increment the reputation for honest identities
            if honest_identity not in self.identities:
                self.identities[honest_identity] = 0
            self.identities[honest_identity] += reputation_to_distribute
            if self.logger:
                self.logger.debug(f"{self.epoch} -- {honest_identity} reputation score has increased by {reputation_to_distribute} points")

            # Insert into database
            self.insert_reputation_difference(honest_identity, self.epoch, reputation_to_distribute, "gain")

            # Track which identities earned reputation
            reputation_earning_identities.append(honest_identity)

        return reputation_to_distribute * len(reputation_earning_identities), reputation_earning_identities

    def update(self, epoch, revealing_identities, honest_identities, error_identities, liar_identities):
        # If we loaded a TRS from a file, check if the sequential epochs make sense
        if self.first_update and abs(self.epoch - epoch) > 10 and self.logger:
            self.logger.warning(f"TRS loaded from JSON file was persisted at epoch {self.epoch}, first update is at {epoch}")
        self.first_update = False

        # Fake reputation expiry because the received epochs are not sequential
        if self.epoch and epoch > self.epoch + 1:
            self.expire_reputation_in_next_epoch()
            # Remove all zero-reputation identities
            self.clean()
            # Save the TRS with expired reputation to our database
            self.insert_trs(next_epoch=True)
        if self.epoch and epoch > self.epoch + 2:
            total_reputation = self.leftover_reputation
            if self.logger:
                self.logger.debug(f"{self.epoch + 2} -- {self.leftover_reputation} from previous epoch + 0 expired + 0 issued + 0 penalized = {total_reputation}")

        # Track the last epoch, do not update this earlier since previous expiries still require the old epoch
        self.epoch = epoch

        honest_identities, liar_identities = self.filter_honest_identities(honest_identities, error_identities, liar_identities)

        # Calculate witnessing acts for this epoch
        new_witnessing_acts = sum(revealing_identities.values())

        if self.logger:
            self.logger.debug(f"{self.epoch} -- Witnessing acts: Total {self.witnessing_acts} + new {new_witnessing_acts}")

        # Calculate expired reputation
        expired_reputation = self.expire_reputation()

        # Calculate newly issued reputation
        issued_reputation = self.issue_reputation(new_witnessing_acts)

        # Calculate penalized reputation
        penalized_reputation = self.penalize_liars(liar_identities)

        # Calculate total reputation
        total_reputation = self.leftover_reputation + expired_reputation + issued_reputation + penalized_reputation
        if self.logger:
            self.logger.debug(f"{self.epoch} -- {self.leftover_reputation} from previous epoch + {expired_reputation} expired + {issued_reputation} issued + {penalized_reputation} penalized = {total_reputation}")

        # Distribute reputation over all honest identities
        total_reputation_distributed, reputation_earning_identities = self.distribute_reputation(total_reputation, honest_identities)

        # Finalize database insertions
        self.finalize_reputation_insertions()

        # Update the reputation gain list
        if total_reputation_distributed > 0:
            reputation_expiry_time = self.witnessing_acts + new_witnessing_acts + self.reputation_expiration
            reputation_per_identity = int(total_reputation_distributed / (len(reputation_earning_identities) or 1))
            self.reputation_expiry.append([reputation_expiry_time, {identity: reputation_per_identity for identity in reputation_earning_identities}])

            # Track statistic
            if reputation_per_identity > self.max_reputation_distributed:
                self.max_reputation_distributed = reputation_per_identity

        # Leftover reputation to distribute next epoch
        self.leftover_reputation = total_reputation - total_reputation_distributed

        # Update the amount of witnessing acts
        self.witnessing_acts += new_witnessing_acts

        # Remove all zero-reputation identities
        self.clean()

        # Save the new TRS to our database
        self.insert_trs()

    #####################################################
    #               Eligibility functions               #
    #####################################################

    # Calculate the result of `y = mx + K`, the result is rounded and low saturated in 0
    def magic_line(self, x, m, k):
        res = m * x + k
        if res < 0:
            return 0
        else:
            return round(res, 0)

    # Calculate the values and the total reputation for the upper triangle of the trapezoid
    def calculate_trapezoid_triangle(self, total_active_rep, active_reputed_ids_len, minimum_rep):
        # Calculate parameters for the curve y = mx + k
        # k: 1'5 * average of the total active reputation without the minimum
        average = total_active_rep / active_reputed_ids_len
        k = 1.5 * (average - minimum_rep)
        # m: negative slope with -k
        m = -k / (active_reputed_ids_len - 1)

        triangle_reputation = []
        total_triangle_reputation = 0
        for i in range(0, active_reputed_ids_len):
            calculated_rep = self.magic_line(i, m, k)
            triangle_reputation.append(calculated_rep)
            total_triangle_reputation += calculated_rep

        return triangle_reputation, total_triangle_reputation

    # Use the trapezoid distribution to calculate eligibility for each of the identities in the ARS based on their reputation ranking
    def trapezoidal_eligibility(self, identities):
        active_reputed_ids = sorted([(identity, reputation) for identity, reputation in identities.items()], key=lambda l: l[1], reverse=True)
        total_active_rep = sum(identities.values())

        if len(active_reputed_ids) == 0:
            return {}, 0

        # Calculate upper triangle reputation in the trapezoidal eligibility
        minimum_rep = active_reputed_ids[-1][1]
        triangle_reputation, total_triangle_reputation = self.calculate_trapezoid_triangle(total_active_rep, len(active_reputed_ids), minimum_rep)

        # To complete the trapezoid, an offset needs to be added (the rectangle at the base)
        remaining_reputation = total_active_rep - total_triangle_reputation
        offset_reputation = remaining_reputation / len(active_reputed_ids)
        ids_with_extra_rep = remaining_reputation % len(active_reputed_ids)

        eligibility = {}
        for i, (ar_id, rep) in enumerate(zip(active_reputed_ids, triangle_reputation)):
            trapezoid_rep = rep + offset_reputation
            if i < ids_with_extra_rep:
                trapezoid_rep += 1
            eligibility[ar_id[0]] = int(trapezoid_rep)

        return eligibility, total_active_rep

    # Calculate actual relative eligibilities adding 1 to each of the identities
    def calculate_eligibilities(self, identities):
        eligibility, total_active_rep = self.trapezoidal_eligibility(identities)

        eligibilities = {}
        for identity in identities.keys():
            eligibilities[identity] = (eligibility.get(identity, 0) + 1) / (total_active_rep + len(identities))

        return eligibilities

    #####################################################
    #                  Helper functions                 #
    #####################################################

    def clean(self):
        self.identities = {identity: reputation for identity, reputation in self.identities.items() if reputation > 0}

    def transform_identities(self):
        # Check if there are any new addresses we need to insert into our mapping table
        addresses_to_insert = []
        for address, reputation in self.identities.items():
            if not self.address_ids.get(address):
                addresses_to_insert.append([address])

        # Do insertion and refresh our local mapping
        if len(addresses_to_insert) > 0:
            self.insert_addresses(addresses_to_insert)
            self.get_addresses_to_ids()

        # Transform addresses list to address of ids
        address_ids, reputations = [], []
        for address, reputation in self.identities.items():
            address_ids.append(self.address_ids[address])
            reputations.append(reputation)

        return address_ids, reputations

    #####################################################
    #                  Print functions                  #
    #####################################################

    def print_trs(self):
        trs_str = "{"
        for identity, reputation in sorted(self.identities.items(), key=lambda l: l[1], reverse=True):
            trs_str += f"\"{identity}\": {reputation}, "
        trs_str = trs_str[:-2] + "}"
        print(trs_str)

    def print_statistics(self):
        print(f"Maximum reputation distributed to a single identity: {self.max_reputation_distributed}")
        print(f"Maximum reputation slashed from a single identity: {self.max_reputation_slashed}")
