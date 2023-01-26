################################################################################
# Copyright IBM Corporation 2021, 2022
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

import re
import sys

import yaml
import json
from neomodel import db
from neomodel import StructuredNode
from collections import OrderedDict
from abc import ABC, abstractmethod
from dgi.models import SQLTable, SQLColumn, MethodNode, ClassNode
from dgi.utils.logging import Log
from dgi.utils.progress_bar_factory import ProgressBarFactory
from ipdb import set_trace

from typing import Dict
from dgi.tx2graph.utils import sqlexp

class AbstractTransactionLoader(ABC):

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def _consume_and_process_label(label: str) -> Dict:
        """Format label into a proper JSON string 

        Args:
        label (str): The label as an unformatted string
        """
        label_raw: str = label

        # -- DiVA's JSON is malformed. Here, we fix those malformations --
        label = re.sub("\n", '', label)
        label = re.sub(" ", '', label)
        label = re.sub("{", "{\"", label)
        label = re.sub(":", "\":", label)
        label = re.sub(",", ",\"", label)
        label = re.sub("\[", '["', label)
        label = re.sub("\]", '"]', label)

        # -- convert to json --
        label = json.loads(label)

        return label

    @staticmethod
    def _clear_all_nodes(force_clear_all: bool):
        """ Delete all nodes """
        Log.warn("The CLI argument clear is turned ON. Deleting pre-existing nodes.")
        db.cypher_query("MATCH (n:SQLTable)-[r]-(m) DELETE r")
        db.cypher_query("MATCH (n)-[r]-(m:SQLTable) DELETE r")
        db.cypher_query("MATCH (n:SQLTable) DELETE n")
        db.cypher_query("MATCH (n:SQLColumn)-[r]-(m) DELETE r")
        db.cypher_query("MATCH (n)-[r]-(m:SQLColumn) DELETE r")
        db.cypher_query("MATCH (n:SQLColumn) DELETE n")
        if force_clear_all:
            Log.warn("Force clear has been turned ON. ALL nodes will be deleted.")
            db.cypher_query("MATCH (n)-[r]-(m) DELETE r")
            db.cypher_query("MATCH (n) DELETE n")

    def crud0(self, ast, write=False):
        if isinstance(ast, list):
            res = [set(), set()]
            for child in ast[1:]:
                rs, ws = self.crud0(child, ast[0] != 'select')
                res[0] |= rs
                res[1] |= ws
            return res
        elif isinstance(ast, dict) and ':from' in ast:
            ts = [list(t.values())[0] if isinstance(t, dict)
                  else t for t in ast[':from'] if not isinstance(t, tuple)]
            res = set()
            for t in ts:
                if isinstance(t, list):
                    res |= self.crud0(t, False)[0]
                else:
                    res.add(t)
            return [set(), res] if write else [res, set()]
        else:
            return [set(), set()]

    def crud(self, sql):
        r = sqlexp(sql.lower())
        if r:
            return self.crud0(r[1])
        else:
            return [set(), set()]

    def analyze(self, txs):
        for tx in txs:
            stack = []
            if tx['transaction'] and tx['transaction'][0]['sql'] != 'BEGIN':
                tx['transaction'] = [{'sql': 'BEGIN'}] + tx['transaction']
            for op in tx['transaction']:
                if op['sql'] == 'BEGIN':
                    stack.append([set(), set()])
                    op['rwset'] = stack[-1]
                elif op['sql'] in ('COMMIT', 'ROLLBACK'):
                    if len(stack) > 1:
                        stack[-2][0] |= stack[-1][0]
                        stack[-2][1] |= stack[-1][1]
                    stack[-1][0] = set(stack[-1][0])
                    stack[-1][1] = set(stack[-1][1])
                    stack.pop()
                else:
                    rs, ws = self.crud(op['sql'])
                    stack[-1][0] |= rs
                    stack[-1][1] |= ws
        return txs

    @abstractmethod
    def find_or_create_program_node(self, method_signature: str) -> StructuredNode:
        """Create an node pertaining to a program feature like class, method, etc. 

        Args:
            method_signature (_type_): The full method method signature
        """
        pass

    @abstractmethod
    def find_or_create_SQL_table_node(self, table_name: str) -> StructuredNode:
        """Create an nodes pertaining to a SQL Table.

        Args:
            table_name (str): The name of the table
        """
        pass

    @abstractmethod
    def populate_transaction_read(self, label: dict, txid: int, table: str) -> None:
        """Add transaction read edges to the database

        Args:
            label (dict): This is a dictionary of the attribute information for the edge. It contains information such 
                          as the entrypoint class, method, etc. 
            txid (int):   This is the ID assigned to the transaction.
            table (str):  The is the name of the table.
        """
        pass

    @abstractmethod
    def populate_transaction_write(self, label: dict, txid: int, table: str) -> None:
        """Add transaction write edges to the database

        Args:
            label (dict): This is a dictionary of the attribute information for the edge. It contains information such 
                          as the entrypoint class, method, etc. 
            txid (int):   This is the ID assigned to the transaction.
            table (str):  The is the name of the table.
        """
        pass

    @abstractmethod
    def populate_transaction(self, label: dict, txid: int, read: str, write: str, transactions: list, action: str):
        """Add transaction write edges to the database

        Args:
            label (dict):        This is a dictionary of the attribute information for the edge. It contains information
                                 such as the entrypoint class, method, etc.
            txid (int):          This is the ID assigned to the transaction.
            read (str):          The name of table that has read operations performed on it.
            write (str):         The name of the table that has the write operations performed on it.
            transactions (str):  A list of all the transactions.
            action (str):        The action that initiated the transaction
        """
        pass

    @abstractmethod
    def populate_transaction_callgraph(self, callstack: dict, tx_id: int, entrypoint: str) -> None:
        """Add transaction write edges to the database

        Args:
            callstack (dict): The callstack from the entrypoint to the transaction.
            tx_id (int)      : This is the ID assigned to the transaction.
            entrypoint (str): The entrypoint that initiated this transaction.
        """
        pass

    def tx2neo4j(self, transactions, label):
        # If there are no transactions to process, nothing to do here.
        if len(transactions) == 0:
            return

        label = self._consume_and_process_label(label)
        entrypoint = label['entry']['methods'][0]
        action = label.get('action')
        if action is not None:
            action = action[tuple(label['action'].keys())[0]][0]

        for transaction_dict in transactions:
            txid = transaction_dict['txid']
            read, write = transaction_dict['transaction'][0]['rwset']
            for each_transaction in transaction_dict['transaction'][1:-1]:  # [0] -> BEGIN, [-1] -> COMMIT
                self.populate_transaction_callgraph(each_transaction['stacktrace'], txid, entrypoint)
                self.populate_transaction(label, txid, read, write, each_transaction, action)

    def load_transactions(self, input, clear, force_clear=False):

        # ----------------------
        # Load transactions data
        # ----------------------
        yaml.add_representer(OrderedDict, lambda dumper, data: dumper.represent_mapping(
            'tag:yaml.org,2002:map', list(data.items())))
        data = json.load(open(input), object_pairs_hook=OrderedDict)

        # --------------------------
        # Remove all existing nodes?
        # --------------------------
        if clear:
            self._clear_all_nodes(force_clear)

        Log.info("{}: Populating transactions".format(type(self).__name__))

        with ProgressBarFactory.get_progress_bar() as p:
            for (c, entry) in p.track(enumerate(data), total=len(data)):
                txs = self.analyze(entry['transactions'])
                del(entry['transactions'])
                label = yaml.dump(entry, default_flow_style=True).strip()
                self.tx2neo4j(txs, label)