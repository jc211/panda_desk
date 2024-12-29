"""
Introduction
------------

panda-py is a Python library for the Franka Emika Robot System
that allows you to program and control the robot in real-time.


"""

import base64
import configparser
import dataclasses
import hashlib
import json as json_module
import logging
import os
import ssl
import typing
from urllib import parse
import httpx
import trio
from trio_websocket import open_websocket_url
from trio_util import trio_async_generator

__version__ = '0.8.1'

_logger = logging.getLogger('desk')
_logger.setLevel(logging.DEBUG)
_logger.addHandler(logging.StreamHandler())

TOKEN_PATH = '~/.panda_py/token.conf'

@dataclasses.dataclass
class Token:
  """
  Represents a Desk token owned by a user.
  """
  id: str = ''
  owned_by: str = ''
  token: str = ''

class Desk:
    """
    Connects to the control unit running the web-based Desk interface
    to manage the robot. Use this class to interact with the Desk
    from Python, e.g. if you use a headless setup. This interface
    supports common tasks such as unlocking the brakes, activating
    the FCI etc.

    Newer versions of the system software use role-based access
    management to allow only one user to be in control of the Desk
    at a time. The controlling user is authenticated using a token.
    The :py:class:`Desk` class saves those token in :py:obj:`TOKEN_PATH`
    and will use them when reconnecting to the Desk, retaking control.
    Without a token, control of a Desk can only be taken, if there is
    no active claim or the controlling user explicitly relinquishes control.
    If the controlling user's token is lost, a user can take control
    forcefully (cf. :py:func:`Desk.take_control`) but needs to confirm
    physical access to the robot by pressing the circle button on the
    robot's Pilot interface.
    """

    def __init__(self,
                hostname: str = "",
                platform: typing.Literal['panda', 'fr3'] = 'panda') -> None:
        
        self._legacy = False
        if platform.lower() in [
            'panda', 'fer', 'franka_emika_robot', 'frankaemikarobot'
        ]:
            self._platform = 'panda'
        elif platform.lower() in ['fr3', 'frankaresearch3', 'franka_research_3']:
            self._platform = 'fr3'
        else:
            raise ValueError("Unknown platform! Must be either 'panda' or 'fr3'!")
        
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._session = httpx.AsyncClient(verify=ctx)

        self._hostname = hostname
        self._platform = platform
        self._logged_in = False
        self._username = 'Not set'
        self._token = self._load_token()
    
    def logged_in(self) -> bool:
        """
        Returns whether the Desk is logged in.
        """
        return self._logged_in  
        
    @staticmethod
    def encode_password(username: str, password: str) -> str:
        """
        Encodes the password into the form needed to log into the Desk interface.
        """
        bytes_str = ','.join([
            str(b) for b in hashlib.sha256((
                f'{password}#{username}@franka').encode('utf-8')).digest()
        ])
        return base64.encodebytes(bytes_str.encode('utf-8')).decode('utf-8')
    
    async def login(self, username: str, password: str) -> None:
        """
        Uses the object's instance parameters to log into the Desk.
        The :py:class`Desk` class's constructor will try to connect
        and login automatically.
        """
        login = await self._request(
            'post',
            '/admin/api/login',
            json={
                'login': username,
                'password': self.encode_password(username, password)
            },
            )
        self._session.cookies.set('authorization', login.text)
        self._logged_in = True
        self._username = username
        _logger.info('Login succesful.')  

    async def logout(self) -> None:
        logout = await self._request(
            'post',
            '/admin/api/logout',
            )
        # self._session.cookies.set('authorization', login.text)
        self._logged_in = False
        _logger.info('Logout succesful.')  
    
    async def set_mode(self, mode: typing.Literal["execution", "programming"]) -> None:
        """
        Uses the object's instance parameters to log into the Desk.
        The :py:class`Desk` class's constructor will try to connect
        and login automatically.
        """
        if self._platform == 'panda':
            print("Old panda platform does not support this function")
            return
        
        if mode == "execution":
            url = '/desk/api/operating-mode/execution'
        elif mode == "programming":
            url = '/desk/api/operating-mode/programming'
        else:
            raise ValueError(f"Unknown mode {mode}")

        req = await self._request(
            'post',
            url,
            timeout=50,
            headers={'X-Control-Token': self._token.token})

        _logger.info(f'Set mode to {mode}.')

                
    async def check_has_control(self):
        token = await self._get_active_token()
        return self._token.id == token.id
    
    async def take_control(self, force: bool = False):
        """
        Takes control of the Desk, generating a new control token and saving it.
        If `force` is set to True, control can be taken forcefully even if another
        user is already in control. However, the user will have to press the circle
        button on the robot's Pilot within an alotted amount of time to confirm
        physical access.

        For legacy versions of the Desk, this function does nothing.
        """
        if self._legacy:
            return True
        active = await self._get_active_token()
        if active.id != '' and self._token.id == active.id:
            _logger.info('Retaken control.')
            return True
        if active.id != '' and not force:
            _logger.warning('Cannot take control. User %s is in control.',
                        active.owned_by)
            return False
        response = await self._request(
            'post',
            f'/admin/api/control-token/request{"?force" if force else ""}',
            json={
                'requestedBy': self._username
            })
        response = response.json()
        if force:
            r = await self._request('get',
                                '/admin/api/safety')
            timeout = r.json()['tokenForceTimeout']
            _logger.warning(
                'You have %d seconds to confirm control by pressing circle button on robot.',
                timeout)
        
            with trio.move_on_after(timeout) as cancel_scope:
                await self.wait_for_press('circle')

            if cancel_scope.cancel_called:
                _logger.warning('Control not confirmed. Giving up.')
                return False
                    
        self._save_token(
            Token(str(response['id']), self._username, response['token']))
        _logger.info('Taken control.')
        return True
    
    @trio_async_generator
    async def robot_states(self):
        """ Returns cartesian pose (16), estimated forces (6), estimated torques (7), and joint angles (7). 
        Example:
        {
            "cartesianPose": [
                0.9921918750166545,
                0.04184658466009511,
                0.11749090294435147,
                0,
                0.0486533690265647,
                -0.9972627463328305,
                -0.05567611903252592,
                0,
                0.11483944904155471,
                0.060957723281242195,
                -0.9915120054322084,
                0,
                0.2923385865363911,
                0.09409859354808087,
                0.5085957823211247,
                1
            ],
            "estimatedForces": [
                0.2162808495091331,
                -0.4345939539865341,
                -0.23250551101884834,
                -0.061200322460370715,
                0.01342586960914791,
                0.06415705090225535
            ],
            "estimatedTorques": [
                0.10187140929146088,
                -0.06991852701336056,
                0.05740853962628282,
                0.18436922111003895,
                -0.0006288486372015669,
                0.09181937009334365,
                0.1028044936597811
            ],
            "jointAngles": [
                -0.2090783682630469,
                -0.9162435760010649,
                0.28992812192930767,
                -2.3619656065492456,
                0.2916468496321751,
                1.584088039562825,
                0.7370122509488684
            ]
        } 
        """

        async with self.connect('/desk/api/robot/configuration') as websocket:
            while True:
                event = await websocket.get_message()
                yield json_module.loads(event)

    @trio_async_generator
    async def general_system_status(self):            
        """
        Example output:
        {
            "execution": {
                "aborted": false,
                "error": null,
                "errorHandling": false,
                "lastActivePath": null,
                "remainingWaitTime": null,
                "running": false,
                "state": {
                    "active": false,
                    "children": [],
                    "exitPort": null,
                    "id": null,
                    "result": null
                },
                "tracking": true
            },
            "safety": {
                "sequenceNumber": 18487175,
                "safetyControllerStatus": "Idle",
                "brakeState": [
                    "Locked", # "Locked" or "Unlocked"
                    "Locked",
                    "Locked",
                    "Locked",
                    "Locked",
                    "Locked",
                    "Locked"
                ],
                "stoState": "SafeTorqueOff",
                "timeToTd2": 8101,
                "activeWarnings": {
                    "safetySettingsInvalidated": false,
                    "temperatureHigh": false
                },
                "demandedRecoveries": {
                    "jointLimitViolation": [
                        false,
                        false,
                        false,
                        false,
                        false,
                        false,
                        false
                    ],
                    "jointPositionError": [
                        false,
                        false,
                        false,
                        false,
                        false,
                        false,
                        false
                    ],
                    "safetyRuleViolationsConfirmation": {},
                    "safetyRuleViolationsRecovery": {}
                },
                "recoverableErrors": {
                    "environmentDataTimeout": false,
                    "fsoeConnectionError": false,
                    "genericJointError": false,
                    "guidingEnablingDevice": false,
                    "jointPositionError": false,
                    "safeInputErrorX31": false,
                    "safeInputErrorX32": false,
                    "safeInputErrorX33": false,
                    "safeInputErrorX4": false,
                    "td2Timeout": false
                },
                "activeRecovery": null,
                "safeInputState": {
                    "guidingEnableButton": "Inactive",
                    "x31": "Active",
                    "x32": "Inactive",
                    "x33": "Inactive",
                    "x4": "Inactive"
                },
                "powerState": {
                    "endEffector": "Off",
                    "robot": "On"
                },
                "safetyControllerStatusReason": {
                    "conflictingInputs": false,
                    "fsoeWatchdogError": false,
                    "sacoVersionMismatch": false,
                    "nonRecoverableSafetyError": false,
                    "temperatureError": false,
                    "connectionToSafetySettingsManagerLost": false,
                    "environmentDataMissing": false,
                    "jointsSafetyError": [
                        false,
                        false,
                        false,
                        false,
                        false,
                        false,
                        false
                    ],
                    "safetyVersionMismatch": false
                },
                "safetyConfigurationIndex": 0,
                "fsoeConnectionStatus": [
                    "Data",
                    "Data",
                    "Data",
                    "Data",
                    "Data",
                    "Data",
                    "Data"
                ]
            },
            "robot": {
                "closeToSingularity": false,
                "endEffectorConfiguration": {
                    "centerOfMass": [
                        -0.009999999776482582,
                        0,
                        0.029999999329447746
                    ],
                    "inertia": [
                        0.0010000000474974513,
                        0,
                        0,
                        0,
                        0.0024999999441206455,
                        0,
                        0,
                        0,
                        0.0017000000225380063
                    ],
                    "mass": 0.7300000190734863,
                    "transformation": [
                        0.7071067690849304,
                        -0.7071067690849304,
                        0,
                        0,
                        0.7071067690849304,
                        0.7071067690849304,
                        0,
                        0,
                        0,
                        0,
                        1,
                        0,
                        0,
                        0,
                        0.10339999943971634,
                        1
                    ]
                },
                "robotErrors": []
            },
            "processes": "Up",
            "startup": {
                "tag": "Started"
            },
            "controlToken": {
                "activeToken": {
                    "id": 645396955,
                    "ownedBy": "admin"
                },
                "fciActive": false,
                "tokenRequest": null
            },
            "derived": {
                "operatingMode": "Programming",
                "desiredColor": {
                    "color": "Blue",
                    "mode": "Constant"
                },
                "td2Tests": {
                    "status": "OK",
                    "canExecute": true
                },
                "lifetime": {
                    "status": "OK",
                    "lifetime": 0.013,
                    "isConfirmationNeeded": false
                }
            }
        }
        """
        async with self.connect('admin/api/system-status') as websocket:
            while True:
                event = await websocket.get_message()
                yield json_module.loads(event)

    @trio_async_generator
    async def safety_status(self):            
        """
        This includes the brakeState (7)
        Example output:

        {
            "sequenceNumber": 18471788,
            "safetyControllerStatus": "Idle",
            "brakeState": [
                "Locked",
                "Locked",
                "Locked",
                "Locked",
                "Locked",
                "Locked",
                "Locked"
            ],
            "stoState": "SafeTorqueOff",
            "timeToTd2": 8252,
            "activeWarnings": {
                "safetySettingsInvalidated": false,
                "temperatureHigh": false
            },
            "demandedRecoveries": {
                "jointLimitViolation": [
                    false,
                    false,
                    false,
                    false,
                    false,
                    false,
                    false
                ],
                "jointPositionError": [
                    false,
                    false,
                    false,
                    false,
                    false,
                    false,
                    false
                ],
                "safetyRuleViolationsConfirmation": {},
                "safetyRuleViolationsRecovery": {}
            },
            "recoverableErrors": {
                "environmentDataTimeout": false,
                "fsoeConnectionError": false,
                "genericJointError": false,
                "guidingEnablingDevice": false,
                "jointPositionError": false,
                "safeInputErrorX31": false,
                "safeInputErrorX32": false,
                "safeInputErrorX33": false,
                "safeInputErrorX4": false,
                "td2Timeout": false
            },
            "activeRecovery": null,
            "safeInputState": {
                "guidingEnableButton": "Inactive",
                "x31": "Active",
                "x32": "Inactive",
                "x33": "Inactive",
                "x4": "Inactive"
            },
            "powerState": {
                "endEffector": "Off",
                "robot": "On"
            },
            "safetyControllerStatusReason": {
                "conflictingInputs": false,
                "fsoeWatchdogError": false,
                "sacoVersionMismatch": false,
                "nonRecoverableSafetyError": false,
                "temperatureError": false,
                "connectionToSafetySettingsManagerLost": false,
                "environmentDataMissing": false,
                "jointsSafetyError": [
                    false,
                    false,
                    false,
                    false,
                    false,
                    false,
                    false
                ],
                "safetyVersionMismatch": false
            },
            "safetyConfigurationIndex": 0,
            "fsoeConnectionStatus": [
                "Data",
                "Data",
                "Data",
                "Data",
                "Data",
                "Data",
                "Data"
            ]
        }
        
        """
        async with self.connect('admin/api/safety/status') as websocket:
            while True:
                event = await websocket.get_message()
                yield json_module.loads(event)

    @trio_async_generator
    async def system_status(self):            
        """
        Example:
        {
            "connectedSlaves": 7,
            "ethernetConnected": true,
            "firmwareDownloadStatus": [
                "INITIAL_STATE",
                "INITIAL_STATE",
                "INITIAL_STATE",
                "INITIAL_STATE",
                "INITIAL_STATE",
                "INITIAL_STATE",
                "INITIAL_STATE"
            ],
            "firmwareVersion": [
                "2.0.7-F",
                "2.0.7-F",
                "2.0.7-F",
                "2.0.7-F",
                "2.0.7-F",
                "2.0.7-F",
                "2.0.7-F"
            ],
            "jointStatus": [
                0,
                0,
                0,
                0,
                0,
                0,
                0
            ],
            "jointsInError": false,
            "lifetimeConfirmationNeeded": false,
            "lifetimePercentages": [
                0,
                0.013,
                0,
                0.001,
                0,
                0,
                0
            ],
            "masterStatus": "OP",
            "slavesOperational": true,
            "startedWithEni": true
        }
        """
        async with self.connect('desk/api/system/status') as websocket:
            while True:
                event = await websocket.get_message()
                yield json_module.loads(event)

    @trio_async_generator
    async def button_events(self):            
        """
        Includes "circle", "check", "cross", "up", "down", "left", "right" keys. It triggers events letting you know
        when a button is pressed or released. It only includes the buttons that have changed state.

        Example:
        {
            "circle": false # means the circle button was just released
        }
        """
        async with self.connect('desk/api/navigation/events') as websocket:
            while True:
                event = await websocket.get_message()
                yield json_module.loads(event)
    
    async def wait_for_brakes_to_open(self):
        async with self.safety_status() as generator:
            async for status in generator:
                brakes_unlocked = [b == "Unlocked" for b in status['brakeState']]
                if all(brakes_unlocked):
                    return
    
    async def wait_for_brakes_to_close(self):
        async with self.safety_status() as generator:
            async for status in generator:
                brakes_locked = [b == "Locked" for b in status['brakeState']]
                if all(brakes_locked):
                    return

    async def wait_for_press(self, button: typing.Literal['circle', 'check', 'cross', 'up', 'down', 'left', 'right']):
        async with self.button_events() as generator:
            async for e in generator:
                if button in e.keys() and e[button] == True:
                    return e
                
    async def wait_for_release(self, button: typing.Literal['circle', 'check', 'cross', 'up', 'down', 'left', 'right']):
        async with self.button_events() as generator:
            async for e in generator:
                if button in e.keys() and e[button] == False:
                    return e
    
    async def lock(self, force: bool = True) -> None:
        """
        Locks the brakes. API call blocks until the brakes are locked.
        """
        if self._platform == 'panda':
            url = '/desk/api/robot/close-brakes'
        elif self._platform == 'fr3':
            url = '/desk/api/joints/lock'
        
        await self._request('post',
                url,
                files={'force': str(force).encode('utf-8')},
                timeout=50,
                headers={'X-Control-Token': self._token.token})
        await self.wait_for_brakes_to_close()
    
    async def unlock(self, force: bool = True) -> None:
        """
        Unlocks the brakes. API call blocks until the brakes are unlocked.
        """
        if self._platform == 'panda':
            url = '/desk/api/robot/open-brakes'
        elif self._platform == 'fr3':
            url = '/desk/api/joints/unlock'
        await self._request('post',
                url,
                files={'force': str(force).encode('utf-8')},
                timeout=50,
                headers={'X-Control-Token': self._token.token})
        await self.wait_for_brakes_to_open()

    async def reboot(self) -> None:
        """
        Reboots the robot hardware (this will close open connections).
        """
        await self._request('post',
                    '/admin/api/reboot',
                    headers={'X-Control-Token': self._token.token})

    async def activate_fci(self) -> None:
        """
        Activates the Franka Research Interface (FCI). Note that the
        brakes must be unlocked first. For older Desk versions, this
        function does nothing.
        """
        if not self._legacy:
            await self._request('post',
                            '/admin/api/control-token/fci',
                            json={'token': self._token.token})

    async def deactivate_fci(self) -> None:
        """
        Deactivates the Franka Research Interface (FCI). For older
        Desk versions, this function does nothing.
        """
        if not self._legacy:
            await self._request('delete',
                        '/admin/api/control-token/fci',
                        json={'token': self._token.token})
        
    async def _get_active_token(self) -> Token:
        token = Token()
        if self._legacy:
            return token
        response = await self._request("get", "/admin/api/control-token")
        response = response.json()
        if response['activeToken'] is not None:
            token.id = str(response['activeToken']['id'])
            token.owned_by = response['activeToken']['ownedBy']
        return token

    
    def _load_token(self) -> Token:
        config_path = os.path.expanduser(TOKEN_PATH)
        config = configparser.ConfigParser()
        token = Token()
        if os.path.exists(config_path):
            config.read(config_path)
            if config.has_section(self._hostname):
                token.id = config.get(self._hostname, 'id')
                token.owned_by = config.get(self._hostname, 'owned_by')
                token.token = config.get(self._hostname, 'token')
        return token

    def _save_token(self, token: Token) -> None:
        config_path = os.path.expanduser(TOKEN_PATH)
        config = configparser.ConfigParser()
        if os.path.exists(config_path):
            config.read(config_path)
        config[self._hostname] = {
            'id': token.id,
            'owned_by': token.owned_by,
            'token': token.token
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as config_file:
            config.write(config_file)
        self._token = token
    
    async def _request(self, 
                    method: typing.Literal["get", "post", "delete"], 
                    url:str, 
                    headers: typing.Optional[typing.Dict[str, str]] = None,
                    json: typing.Optional[typing.Dict[str, str]]  = None,
                    files: typing.Optional[typing.Dict[str, str]]  = None,
                    timeout: int = 5) -> httpx.Response:
  
        url = parse.urljoin(f"https://{self._hostname}", url)
        fun = getattr(self._session, method)
        kwargs = {}
        if method != 'get':
            kwargs['json'] = json
            kwargs['files'] = files
        kwargs['headers'] = headers
        
        response = await fun(
            url,
            timeout=timeout,
            **kwargs
            )
        if response.status_code != 200:
            print(f"Attemped to connect to {url} with method {method} and got response {response.text}")
            raise ConnectionError(response.text)
        return response
        
    def connect(self, address):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = [("authorization", f"{self._session.cookies.get('authorization')}")]
        res = open_websocket_url(f"wss://{self._hostname}/{address}", ssl_context=ctx, extra_headers=headers)
        return res