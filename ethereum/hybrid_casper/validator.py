import copy
from ethereum.hybrid_casper import casper_utils, chain
from ethereum.pow.ethpow import Miner
from ethereum.tools import tester
from ethereum.meta import make_head_candidate
from ethereum import block, transactions
from ethereum.transaction_queue import TransactionQueue
from ethereum.messages import apply_transaction
from ethereum import abi, utils
from ethereum.slogging import get_logger

log = get_logger('eth.validator')
# config_string = ':info,eth.vm.log:trace,eth.vm.op:trace,eth.vm.stack:trace,eth.vm.exit:trace,eth.pb.msg:trace,eth.pb.tx:debug'
# configure_logging(config_string=config_string)

class Network(object):
    def __init__(self):
        self.nodes = []
        self.time = 0

    def broadcast(self, msg):
        for n in self.nodes:
            n.on_receive(msg)

    def join(self, node):
        self.nodes.append(node)

class Validator(object):
    def __init__(self, key, genesis, network, valcode_addr=None, mining=False):
        self.key = key
        self.coinbase = utils.privtoaddr(self.key)
        self.chain = chain.Chain(genesis=genesis, reset_genesis=True, coinbase=self.coinbase, new_head_cb=self._on_new_head)
        self.mining = mining
        self.nonce = self.chain.state.get_nonce(self.coinbase)
        self.valcode_tx = None
        self.deposit_tx = None
        self.valcode_addr = valcode_addr
        self.votes = dict()
        self.epoch_length = self.chain.env.config['EPOCH_LENGTH']
        # When the transaction_queue is modified, we must set
        # self._head_candidate_needs_updating to True in order to force the
        # head candidate to be updated.
        self.transaction_queue = TransactionQueue()
        self._head_candidate_needs_updating = True
        # Add validator to the network
        self.network = network
        self.network.join(self)

    @property
    def head_candidate(self):
        if self._head_candidate_needs_updating:
            self._head_candidate_needs_updating = False
            # Make a copy of self.transaction_queue because
            # make_head_candidate modifies it.
            txqueue = copy.deepcopy(self.transaction_queue)
            self._head_candidate, self._head_candidate_state = make_head_candidate(
                self.chain, txqueue=txqueue, timestamp=self.chain.state.timestamp + 14)
        return self._head_candidate

    def _on_new_head(self, block):
        self.transaction_queue = self.transaction_queue.diff(block.transactions)
        self._head_candidate_needs_updating = True

    def epoch_blockhash(self, state, epoch):
        if epoch == 0:
            return b'\x00' * 32
        return state.prev_headers[epoch*self.epoch_length * -1 - 1].hash

    def get_recommended_casper_msg_contents(self, casper, validator_index):
        return casper.recommended_target_hash(), casper.current_epoch(), casper.recommended_source_epoch()

    def get_validator_index(self, state):
        t = tester.State(state.ephemeral_clone())
        t.state.gas_limit = 9999999999
        casper = tester.ABIContract(t, casper_utils.casper_abi, self.chain.casper_address)
        if self.valcode_addr is None:
            raise Exception('Valcode address not set')
        try:
            return casper.validator_indexes(self.coinbase)
        except tester.TransactionFailed:
            return None

    # Check the state, and determine if we should commit or prepare
    def on_receive(self, msg):
        if isinstance(msg, block.Block):
            self.accept_block(msg)
        elif isinstance(msg, transactions.Transaction):
            self.accept_transaction(msg)

    def accept_block(self, block):
        self.chain.process_time_queue()
        if not self.chain.add_block(block):
            return
        # Verify this block is a part of our head chain
        if block != self.chain.get_block_by_number(block.header.number):
            return
        # Verify this block is far enough in our epoch
        if block.header.number % self.epoch_length < self.epoch_length // 3:
            return
        # Block is part of the head chain, so attempt to prepare & commit:
        # Create a poststate based on the blockhash we recieved
        post_state = self.chain.mk_poststate_of_blockhash(block.hash)
        post_state.gas_limit = 9999999999999
        # Generate prepare & commit messages and broadcast if possible
        vote_msg = self.generate_vote_message(post_state)
        if vote_msg:
            vote_tx = self.mk_vote_tx(vote_msg)
            self.broadcast_transaction(vote_tx)

    def accept_transaction(self, tx):
        self.transaction_queue.add_transaction(tx)
        if self.mining:
            log.info('Mining tx: {}'.format(tx))
            self.mine_and_broadcast_blocks(1)

    def broadcast_transaction(self, tx):
        log.info('Broadcasting transaction {} from validator {}'.format(str(tx), utils.encode_hex(self.valcode_addr)))
        self.network.broadcast(tx)

    def broadcast_newblock(self, block):
        log.info('Broadcasting block with hash: %s and txs: %s' % (utils.encode_hex(block.hash), str(block.transactions)))
        self.network.broadcast(block)

    def generate_vote_message(self, state):
        epoch = state.block_number // self.epoch_length
        # NO_DBL_VOTE: Don't vote if we have already
        if epoch in self.votes:
            return None
        # TODO: Check for NO_SURROUND_VOTE
        # Create a Casper contract which we can use to get related values
        casper = tester.ABIContract(tester.State(state), casper_utils.casper_abi, self.chain.casper_address)
        # Get the ancestry hash and source ancestry hash
        validator_index = self.get_validator_index(state)
        target_hash, epoch, source_epoch = self.get_recommended_casper_msg_contents(casper, validator_index)
        vote_msg = casper_utils.mk_vote(validator_index, target_hash, epoch, source_epoch, self.key)
        try:  # Attempt to submit the vote, to make sure that it is justified
            casper.vote(vote_msg)
        except tester.TransactionFailed:
            log.info('Vote failed! Validator {} - validator start {} - valcode addr {}'
                     .format(self.get_validator_index(state),
                             casper.validators__start_dynasty(validator_index),
                             utils.encode_hex(self.valcode_addr)))
            return None
        # Save the vote message we generated
        self.votes[epoch] = vote_msg
        log.info('Vote submitted: validator %d - epoch %d - source_epoch %d - hash %s' %
                 (self.get_validator_index(state), epoch, source_epoch, utils.encode_hex(self.epoch_blockhash(state, epoch))))
        return vote_msg

    def mine_and_broadcast_blocks(self, number_of_blocks=1):
        for i in range(number_of_blocks):
            self._head_candidate_needs_updating = True
            block = Miner(self.head_candidate).mine(rounds=100, start_nonce=0)
            self.transaction_queue = self.transaction_queue.diff(block.transactions)
            self.broadcast_newblock(block)

    def broadcast_deposit(self):
        if not self.valcode_tx or not self.deposit_tx:
            # Generate transactions
            valcode_tx = self.mk_validation_code_tx()
            valcode_addr = utils.mk_contract_address(self.coinbase, self.nonce-1)
            deposit_tx = self.mk_deposit_tx(3 * 10**18, valcode_addr)
            # Verify the transactions pass
            temp_state = self.chain.state.ephemeral_clone()
            valcode_success, o1 = apply_transaction(temp_state, valcode_tx)
            deposit_success, o2 = apply_transaction(temp_state, deposit_tx)
            if not (valcode_success and deposit_success):
                self.nonce = self.chain.state.get_nonce(self.coinbase)
                raise Exception('Valcode tx or deposit tx failed')
            self.valcode_tx = valcode_tx
            log.info('Valcode Tx generated: {}'.format(str(valcode_tx)))
            self.valcode_addr = valcode_addr
            self.deposit_tx = deposit_tx
            log.info('Deposit Tx generated: {}'.format(str(deposit_tx)))
        self.broadcast_transaction(self.valcode_tx)
        self.broadcast_transaction(self.deposit_tx)

    def broadcast_logout(self, login_logout_flag):
        epoch = self.chain.state.block_number // self.epoch_length
        # Generage the message
        logout_msg = casper_utils.mk_logout(self.get_validator_index(self.chain.state), epoch, self.key)
        # Generate transactions
        logout_tx = self.mk_logout(logout_msg)
        # Verify the transactions pass
        temp_state = self.chain.state.ephemeral_clone()
        logout_success, o1 = apply_transaction(temp_state, logout_tx)
        if not logout_success:
            self.nonce = self.chain.state.get_nonce(self.coinbase)
            raise Exception('Valcode tx or deposit tx failed')
        log.info('Login/logout Tx generated: {}'.format(str(logout_tx)))
        self.broadcast_transaction(logout_tx)

    def mk_transaction(self, to=b'\x00' * 20, value=0, data=b'', gasprice=tester.GASPRICE, startgas=tester.STARTGAS):
        tx = transactions.Transaction(self.nonce, gasprice, startgas, to, value, data).sign(self.key)
        self.nonce += 1
        return tx

    def mk_validation_code_tx(self):
        valcode_tx = self.mk_transaction('', 0, casper_utils.mk_validation_code(self.coinbase))
        return valcode_tx

    def mk_deposit_tx(self, value, valcode_addr):
        casper_ct = abi.ContractTranslator(casper_utils.casper_abi)
        deposit_func = casper_ct.encode('deposit', [valcode_addr, self.coinbase])
        deposit_tx = self.mk_transaction(self.chain.casper_address, value, deposit_func)
        return deposit_tx

    def mk_logout(self, login_logout_msg):
        casper_ct = abi.ContractTranslator(casper_utils.casper_abi)
        logout_func = casper_ct.encode('logout', [login_logout_msg])
        logout_tx = self.mk_transaction(self.chain.casper_address, data=logout_func)
        return logout_tx

    def mk_vote_tx(self, vote_msg):
        casper_ct = abi.ContractTranslator(casper_utils.casper_abi)
        vote_func = casper_ct.encode('vote', [vote_msg])
        vote_tx = self.mk_transaction(to=self.chain.casper_address, value=0, data=vote_func)
        return vote_tx
