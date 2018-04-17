from ethereum.tools import tester
from ethereum.utils import encode_hex, privtoaddr
from ethereum.hybrid_casper import casper_utils
from ethereum.hybrid_casper.config import casper_config
import re

ALLOC = {a: {'balance': 500*10**24} for a in tester.accounts[:10]}

class Validator(object):
    def __init__(self, withdrawal_addr, key):
        self.withdrawal_addr = withdrawal_addr
        self.key = key
        self.vote_map = {}  # {epoch: vote in that epoch}
        self.double_vote_evidence = []
        self.surrounding_vote_evidence = []

    def get_recommended_casper_msg_contents(self, casper, validator_index):
        return casper.recommended_target_hash(), casper.current_epoch(), casper.recommended_source_epoch()

    def get_vote_msg(self, vote):
        return casper_utils.mk_vote(vote['index'], vote['hash'], vote['target'], vote['source'], vote['key'])

    def vote(self, casper):
        validator_index = self.get_validator_index(casper)
        _h, _t, _s = self.get_recommended_casper_msg_contents(casper, validator_index)
        vote = {'index': validator_index, 'hash': _h, 'target': _t, 'source': _s, 'key': self.key}
        vote_msg = self.get_vote_msg(vote)
        casper.vote(vote_msg)
        # Double vote slash detection
        if _t in self.vote_map and self.vote_map[_t] != vote_msg:
            print('Found double vote for validator:', encode_hex(self.withdrawal_addr))
            self.double_vote_evidence.extend([self.get_vote_msg(self.vote_map[_t]), vote_msg])
        # Surrounding slash detection
        for key, v in self.vote_map.items():
            if (vote['target'] > v['target'] and vote['source'] < v['source']) or \
               (v['target'] > vote['target'] and v['source'] < vote['source']):
                print('Found surrounding vote for validator:', encode_hex(self.withdrawal_addr))
                conflicting_vote = self.get_vote_msg(v)
                self.surrounding_vote_evidence.extend([vote_msg, conflicting_vote])
        # Add vote to vote map
        self.vote_map[_t] = vote

    def slash(self, casper):
        if len(self.double_vote_evidence) > 0:
            print('Slashed double vote')
            casper.slash(self.double_vote_evidence[0], self.double_vote_evidence[1])
        elif len(self.surrounding_vote_evidence) > 0:
            print('Slashed surrounding vote')
            casper.slash(self.surrounding_vote_evidence[0], self.surrounding_vote_evidence[1])
        else:
            raise Exception('No slash evidence found')
        print('Slashed validator:', encode_hex(self.withdrawal_addr))

    def get_validator_index(self, casper):
        if self.withdrawal_addr is None:
            raise Exception('Valcode address not set')
        try:
            return casper.validator_indexes(self.withdrawal_addr)
        except tester.TransactionFailed:
            return None

class TestLangHybrid(object):
    # For a custom Casper parser, overload generic parser and construct your chain
    def __init__(self, epoch_length, withdrawal_delay, dynasty_logout_delay,
        base_interest_factor, base_penalty_factor, deposit_size=2000 * 10**18):
        self.genesis = casper_utils.make_casper_genesis(ALLOC, epoch_length, withdrawal_delay, dynasty_logout_delay,
            base_interest_factor, base_penalty_factor)
        self.t = tester.Chain(genesis=self.genesis)
        self.casper = tester.ABIContract(self.t, casper_utils.casper_abi, self.t.chain.env.config['CASPER_ADDRESS'])
        self.saved_blocks = dict()
        self.validators = dict()
        self.deposit_size = deposit_size
        # Register token handlers
        self.handlers = dict()
        self.handlers['B'] = self.mine_blocks
        self.handlers['J'] = self.join
        self.handlers['V'] = self.vote
        self.handlers['S'] = self.save_block
        self.handlers['R'] = self.revert_to_block
        self.handlers['H'] = self.check_head_equals_block
        self.handlers['X'] = self.slash

    def mine_blocks(self, number):
        if number == '':
            print ("No number of blocks specified, Mining 1 epoch to curr HEAD")
            self.mine_epochs(number_of_epochs=1)
            print('Epoch: {}'.format(self.casper.current_epoch()))
            print('Dynasty: {}'.format(self.casper.dynasty_in_epoch(self.casper.current_epoch())))
        else:
            print ("Mining " + str(number) + " blocks to curr HEAD")
            self.t.mine(number)

    def join(self, number):
        withdrawal_addr = privtoaddr(tester.keys[number])
        casper_utils.induct_validator(self.t, self.casper, tester.keys[number], self.deposit_size)
        self.validators[number] = Validator(withdrawal_addr, tester.keys[number])
        print('Joined validator {}'.format(number))

    def vote(self, validator_index):
        print('New Vote: CurrDynDeposits: {} - Prev Justified: {} - Prev Finalized: {} - Latest nonvoter rescale: {} - Latest voter rescale {}'.format(
            self.casper.total_curdyn_deposits_scaled(), self.casper.recommended_source_epoch(),
            self.casper.last_finalized_epoch(), self.casper.last_nonvoter_rescale(), self.casper.last_voter_rescale()))
        if self.casper.total_curdyn_deposits_scaled() > 0 and self.casper.total_prevdyn_deposits_scaled() > 0:
            print('Vote frac: {}'.format(self.casper.main_hash_voted_frac()))
        self.validators[validator_index].vote(self.casper)

    def slash(self, validator_index):
        self.validators[validator_index].slash(self.casper)

    def save_block(self, saved_block_id):
        if saved_block_id in self.saved_blocks:
            raise Exception('Checkpoint {} already exists'.format(saved_block_id))
        blockhash = self.t.head_state.prev_headers[0].hash
        self.saved_blocks[saved_block_id] = blockhash
        print('Saving checkpoint with hash: {}'.format(encode_hex(self.saved_blocks[saved_block_id])))

    def revert_to_block(self, saved_block_id):
        if saved_block_id not in self.saved_blocks:
            raise Exception('Checkpoint {} does not exist'.format(saved_block_id))
        blockhash = self.saved_blocks[saved_block_id]
        self.t.change_head(blockhash)
        print('Reverting to checkpoint with hash: {}'.format(encode_hex(self.saved_blocks[saved_block_id])))

    def check_head_equals_block(self, saved_block_id):
        if saved_block_id not in self.saved_blocks:
            raise Exception('Checkpoint {} does not exist'.format(saved_block_id))
        blockhash = self.saved_blocks[saved_block_id]
        print('Saved num: {} - Chain head num: {}'.format(self.t.chain.get_block(blockhash).number, self.t.chain.head.number))
        assert self.t.chain.head_hash == blockhash
        print('Passed assert H{}'.format(saved_block_id))

    def parse(self, test_string):
        if test_string == '':
            raise Exception("Please pass in a valid test string")
        for token in test_string.split(' '):
            letter, number = re.match('([A-Za-z]*)([0-9]*)', token).groups()
            if letter+number != token:
                raise Exception("Bad token: %s" % token)
            if number != '':
                number = int(number)
            self.handlers[letter](number)

    # Mines blocks required for number_of_epochs epoch changes, plus an offset of 2 blocks
    def mine_epochs(self, number_of_epochs):
        epoch_length = self.t.chain.config['EPOCH_LENGTH']
        distance_to_next_epoch = (epoch_length - self.t.head_state.block_number) % epoch_length
        number_of_blocks = distance_to_next_epoch + epoch_length*(number_of_epochs-1) + 2
        return self.t.mine(number_of_blocks=number_of_blocks)
