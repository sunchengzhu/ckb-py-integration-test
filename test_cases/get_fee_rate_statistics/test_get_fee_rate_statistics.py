import pytest

from framework.basic import CkbTest


class TestGetFeeRateStatistics(CkbTest):

    @classmethod
    def setup_class(cls):
        cls.node = cls.CkbNode.init_dev_by_port(
            cls.CkbNodeConfigPath.CURRENT_TEST, "telnet1/node", 8114, 8115
        )
        cls.node.prepare()
        cls.node.start()
        cls.Miner.make_tip_height_number(cls.node, 50)

    @classmethod
    def teardown_class(cls):
        cls.node.stop()
        cls.node.clean()
        print("\nTeardown TestClass1")

    def test_01_calculation_logic(self):
        """
        	1.	Send three consecutive transactions, with free_rate of 1000, 2000, and 7000 respectively.
        :return:
            The mean returned by get_fee_rate_statics is (1000+2000+7000)/3=3333, and the median is 2000.
        """
        # build Tx
        account1 = self.Ckb_cli.util_key_info_by_private_key(
            self.Config.ACCOUNT_PRIVATE_1
        )

        fee_rates = [1000, 2000, 7000]
        for fee_rate in fee_rates:
            tx_hash = self.Ckb_cli.wallet_transfer_by_private_key(
                self.Config.ACCOUNT_PRIVATE_1,
                account1["address"]["testnet"],
                140,
                self.node.client.url,
                fee_rate=fee_rate,
            )
            self.node.getClient().get_pool_tx_detail_info(tx_hash)
            self.Miner.miner_until_tx_committed(self.node, tx_hash)
        ret = self.node.getClient().get_fee_rate_statics()
        # 0xd05 = 3333, 0x7d0 = 2000
        assert ret["mean"] == "0xd05"
        assert ret["median"] == "0x7d0"

    def test_02_statistical_range(self):
        """
        	1.  If there are no transactions in the most recent block, get_fee_rate_statics should return null.
            2.	Send a transaction with a free_rate of 1000, and immediately after this transaction is confirmed on the blockchain, call get_fee_rate_statics. Both mean and median should be 1000.
            3.	After waiting for 10 blocks, send a transaction with a free_rate of 2000. Immediately after this transaction is confirmed, call get_fee_rate_statics. Both mean and median should be 1500, indicating that the statistics included both the transaction with free_rate = 1000 and free_rate = 2000.
            4.	Wait another 10 blocks and then call get_fee_rate_statics. Both mean and median should now be 2000, indicating that only the transaction with free_rate = 2000 was counted.
            5.	Wait 21 blocks after the transaction with a free_rate of 2000 is confirmed, then call get_fee_rate_statics. The result should be null, indicating that the transaction with free_rate = 2000 is no longer within the statistical range.
        :return:
        """
        account1 = self.Ckb_cli.util_key_info_by_private_key(
            self.Config.ACCOUNT_PRIVATE_1
        )
        account2 = self.Ckb_cli.util_key_info_by_private_key(
            self.Config.ACCOUNT_PRIVATE_2
        )

        # 1. If there are no transactions in the most recent block, get_fee_rate_statics should return null.
        ret0 = self.node.getClient().get_fee_rate_statics()
        print("ret0: ", ret0)
        assert ret0 is None

        # 2. Send a transaction with a free_rate of 1000, and immediately after this transaction is confirmed on the blockchain, call get_fee_rate_statics. Both mean and median should be 1000.
        tx_hash1 = self.Ckb_cli.wallet_transfer_by_private_key(
            self.Config.ACCOUNT_PRIVATE_1,
            account1["address"]["testnet"],
            140,
            self.node.client.url,
            fee_rate=1000,
        )
        self.Miner.miner_until_tx_committed(self.node, tx_hash1)
        block_number1 = self.node.getClient().get_tip_block_number()
        ret1 = self.node.getClient().get_fee_rate_statics()
        print("ret1: ", ret1, block_number1)
        # 0x3e8 = 1000
        assert ret1["mean"] == "0x3e8"
        assert ret1["median"] == "0x3e8"

        # 3. After waiting for 10 blocks, send a transaction with a free_rate of 2000. Immediately after this transaction is confirmed, call get_fee_rate_statics.
        # Both mean and median should be 1500, indicating that the statistics included both the transaction with free_rate = 1000 and free_rate = 2000.
        self.Miner.make_tip_height_number(self.node, block_number1 + 10)

        tx_hash2 = self.Ckb_cli.wallet_transfer_by_private_key(
            self.Config.ACCOUNT_PRIVATE_2,
            account2["address"]["testnet"],
            140,
            self.node.client.url,
            fee_rate=2000,
        )
        self.Miner.miner_until_tx_committed(self.node, tx_hash2)
        block_number2 = self.node.getClient().get_tip_block_number()
        ret2 = self.node.getClient().get_fee_rate_statics()
        print("ret2: ", ret2, block_number2)
        # 0x5dc = 1500
        assert ret2["mean"] == "0x5dc"
        assert ret2["median"] == "0x5dc"

        # 4. Wait another 10 blocks and then call get_fee_rate_statics. Both mean and median should now be 2000, indicating that only the transaction with free_rate = 2000 was counted.
        self.Miner.make_tip_height_number(self.node, block_number2 + 10)
        block_number3 = self.node.getClient().get_tip_block_number()
        ret3 = self.node.getClient().get_fee_rate_statics()
        print("ret3: ", ret3, block_number3)
        # 0x7d0 = 2000
        assert ret3["mean"] == "0x7d0"
        assert ret3["median"] == "0x7d0"

        # 5. Wait 21 blocks after the transaction with a free_rate of 2000 is confirmed, then call get_fee_rate_statics.
        # The result should be null, indicating that the transaction with free_rate = 2000 is no longer within the statistical range.
        self.Miner.make_tip_height_number(self.node, block_number2 + 21)
        block_number4 = self.node.getClient().get_tip_block_number()
        ret4 = self.node.getClient().get_fee_rate_statics()
        print("ret4: ", ret4, block_number4)
        assert ret4 is None
