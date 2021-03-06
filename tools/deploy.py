#!/usr/bin/env python
import json
import os

import click
import structlog
from eth_utils import to_checksum_address

from raiden.log_config import configure_logging
from raiden.network.rpc.client import JSONRPCClient
from raiden.ui.cli import prompt_account
from raiden.utils import get_contract_path
from raiden.utils.solc import compile_files_cwd

log = structlog.get_logger(__name__)


# Source files for all to be deployed solidity contracts
RAIDEN_CONTRACT_FILES = [
    'NettingChannelLibrary.sol',
    'ChannelManagerLibrary.sol',
    'Registry.sol',
    'EndpointRegistry.sol',
]

# Top level contracts to be deployed. Dependencies are handled automatically
# in `JSONRPCClient.deploy_solidity_contract()`
CONTRACTS_TO_DEPLOY = [
    'Registry.sol:Registry',
    'EndpointRegistry.sol:EndpointRegistry',
]


def patch_deploy_solidity_contract():
    """
    Patch `JSONRPCClient.deploy_solidity_contract()` to not create a copy of
    the `libraries` dict parameter by removing the assignment in
    `raiden.network.rpc.client.py:474` via AST manipulation.

    This allows us to access the addresses of the deployed dependencies until
    the rpc client is fixed.
    """

    import ast
    from inspect import getsource, getsourcefile
    from textwrap import dedent

    class RemoveLibraryDeref(ast.NodeTransformer):
        """
        Removes the AST node representing the line
        `    libraries = dict(libraries)`
        """
        def visit_Assign(self, node):  # pylint: disable=no-self-use
            is_libraries = (
                len(node.targets) == 1 and
                node.col_offset == 4 and
                isinstance(node.targets[0], ast.Name) and
                node.targets[0].id == 'libraries'
            )

            if is_libraries:
                return None

            return node

    ast_ = ast.parse(dedent(getsource(JSONRPCClient.deploy_solidity_contract)))
    ast_ = RemoveLibraryDeref().visit(ast_)
    code = compile(ast_, getsourcefile(JSONRPCClient.deploy_solidity_contract), 'exec')
    ctx = {}

    exec(  # pylint: disable=exec-used
        code,
        JSONRPCClient.deploy_solidity_contract.__globals__,
        ctx,
    )

    JSONRPCClient.deploy_solidity_contract = ctx['deploy_solidity_contract']


def name_from_file(filename):
    return os.path.split(filename)[-1].partition('.')[0]


def deploy_file(contract, compiled_contracts, client):
    libraries = dict()
    filename, _, name = contract.partition(":")
    log.info(f"Deploying {name}")
    proxy = client.deploy_solidity_contract(
        name,
        compiled_contracts,
        libraries,
        '',
        contract_path=filename,
    )

    log.info(f"Deployed {name} @ {to_checksum_address(proxy.contract_address)}")
    libraries[contract] = proxy.contract_address
    return libraries


def deploy_all(client):
    contracts_expanded = [
        get_contract_path(x)
        for x in RAIDEN_CONTRACT_FILES
    ]
    compiled_contracts = compile_files_cwd(contracts_expanded)
    deployed = {}
    for contract in CONTRACTS_TO_DEPLOY:
        deployed.update(deploy_file(contract, compiled_contracts, client))
    return deployed


def get_privatekey(keystore_path):
    address_hex, privatekey = prompt_account(None, keystore_path, None)
    return privatekey


@click.command(help="Deploy the Raiden smart contracts.\n\n"
                    "Requires the private key to an account with enough balance to deploy all "
                    "four contracts.")
@click.option("--pretty", is_flag=True)
@click.option("--gas-price", default=4, help="Gas price to use in GWei", show_default=True)
@click.option("--port", type=int, default=8545, show_default=True)
@click.option("--keystore-path", type=click.Path(exists=True))
def main(keystore_path, pretty, gas_price, port):
    configure_logging({'': 'DEBUG'}, colorize=True)

    privatekey = get_privatekey(keystore_path)

    gas_price_in_wei = gas_price * 1000000000
    patch_deploy_solidity_contract()
    host = '127.0.0.1'
    client = JSONRPCClient(
        host,
        port,
        privatekey,
        gas_price_in_wei,
    )

    deployed = deploy_all(client)

    deployed['EndpointRegistry.sol:EndpointRegistry'] = to_checksum_address(
        deployed['EndpointRegistry.sol:EndpointRegistry'],
    )
    deployed['Registry.sol:Registry'] = to_checksum_address(
        deployed['Registry.sol:Registry'],
    )

    print(json.dumps(deployed, indent=2 if pretty else None))


if __name__ == '__main__':
    main()  # pylint: disable=no-value-for-parameter
