#!/usr/bin/env python3

import boa
import requests
from collections import namedtuple
from networks import ARBITRUM as NETWORK
from networks import PINATA_TOKEN


DAO_FACTORY = "0x49e04AB7af7A263b8ac802c1cAe22f5b4E4577Cd"
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DAO_DESCRIPTION = {
    'name': 'Test YB DAO',
    'description': '',
    'links': []
}

DAO_FACTORY_ABI = """
[{"inputs":[{"components":[{"internalType":"address","name":"trustedForwarder","type":"address"},{"internalType":"string","name":"daoURI","type":"string"},{"internalType":"string","name":"subdomain","type":"string"},{"internalType":"bytes","name":"metadata","type":"bytes"}],"internalType":"struct DAOFactory.DAOSettings","name":"_daoSettings","type":"tuple"},{"components":[{"components":[{"components":[{"internalType":"uint8","name":"release","type":"uint8"},{"internalType":"uint16","name":"build","type":"uint16"}],"internalType":"struct PluginRepo.Tag","name":"versionTag","type":"tuple"},{"internalType":"contract PluginRepo","name":"pluginSetupRepo","type":"address"}],"internalType":"struct PluginSetupRef","name":"pluginSetupRef","type":"tuple"},{"internalType":"bytes","name":"data","type":"bytes"}],"internalType":"struct DAOFactory.PluginSettings[]","name":"_pluginSettings","type":"tuple[]"}],"name":"createDao","outputs":[{"internalType":"contract DAO","name":"createdDao","type":"address"},{"components":[{"internalType":"address","name":"plugin","type":"address"},{"components":[{"internalType":"address[]","name":"helpers","type":"address[]"},{"components":[{"internalType":"enum PermissionLib.Operation","name":"operation","type":"uint8"},{"internalType":"address","name":"where","type":"address"},{"internalType":"address","name":"who","type":"address"},{"internalType":"address","name":"condition","type":"address"},{"internalType":"bytes32","name":"permissionId","type":"bytes32"}],"internalType":"struct PermissionLib.MultiTargetPermission[]","name":"permissions","type":"tuple[]"}],"internalType":"struct IPluginSetup.PreparedSetupData","name":"preparedSetupData","type":"tuple"}],"internalType":"struct DAOFactory.InstalledPlugin[]","name":"installedPlugins","type":"tuple[]"}],"stateMutability":"nonpayable","type":"function"}]
"""

DAO_ABI = """
[
  {
    "inputs": [
      {
        "internalType": "bytes32",
        "name": "_callId",
        "type": "bytes32"
      },
      {
        "components": [
          {
            "internalType": "address",
            "name": "to",
            "type": "address"
          },
          {
            "internalType": "uint256",
            "name": "value",
            "type": "uint256"
          },
          {
            "internalType": "bytes",
            "name": "data",
            "type": "bytes"
          }
        ],
        "internalType": "struct IDAO.Action[]",
        "name": "_actions",
        "type": "tuple[]"
      },
      {
        "internalType": "uint256",
        "name": "_allowFailureMap",
        "type": "uint256"
      }
    ],
    "name": "execute",
    "outputs": [
      {
        "internalType": "bytes[]",
        "name": "execResults",
        "type": "bytes[]"
      },
      {
        "internalType": "uint256",
        "name": "failureMap",
        "type": "uint256"
      }
    ],
    "stateMutability": "nonpayable",
    "type": "function"
  }
]
"""

DaoSettings = namedtuple('DaoSettings', ['trustedForwarder', 'daoURI', 'subdomain', 'metadata'])


def pin_to_ipfs(content: dict):
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "Authorization": f"Bearer {PINATA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "pinataContent": content,
        "pinataMetadata": {"name": "pinnie.json"},
        "pinataOptions": {"cidVersion": 1},
    }

    response = requests.request("POST", url, json=payload, headers=headers)
    assert 200 <= response.status_code < 400

    return 'ipfs://' + response.json()["IpfsHash"]


if __name__ == '__main__':
    boa.fork(NETWORK)
    boa.env.eoa = DEPLOYER

    factory = boa.loads_abi(DAO_FACTORY_ABI, name="DaoFactory").at(DAO_FACTORY)
    dao, _ = factory.createDao(
        DaoSettings(
            trustedForwarder=ZERO_ADDRESS,
            daoURI="",
            subdomain="",
            metadata=pin_to_ipfs(DAO_DESCRIPTION).encode()
        ),
        []
    )
    dao = boa.loads_abi(DAO_ABI, name="DAO").at(dao)
    print(dao)
