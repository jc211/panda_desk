# panda_desk
A library to access the Franka Emika's desk api. The base code was taken from [panda-py](https://github.com/JeanElsner/panda-py). Additions to the base code include making the code asynchronous using [trio](https://github.com/python-trio/trio), adding streams that report the robot's state through the web api, and making this its own package.


## Installation
```
pip install git+https://github.com/jc211/panda_desk
```
## Example


```python
import trio
from panda_desk import Desk

async def main():
    robot_ip = "172.16.0.2" # Change this to your robot's IP address
    desk = Desk(robot_ip, platform="fr3") # For the new Franka 3
    # desk = Desk(robot_ip, platform="panda") #  For the old robots

    username = "admin" # Your admin username
    password = "your_password" # Your password

    await desk.login(username=username, password=password) 
    await desk.take_control(force=True) # You may have to press the circle button on the robot to force control
    await desk.activate_fci()
    await desk.set_mode('programming') # 'execution' for running through fci
    await desk.unlock()

    print("Press circle button on robot. You have 10 seconds")
    with trio.move_on_after(10) as ctx:
        await desk.wait_for_press('circle')

    if ctx.cancel_called:
        print("You didn't press the circle button")
    else:
        print("You pressed the circle button")


if __name__ == "__main__":
    trio.run(main)
```
