from panda_desk import Desk
import trio
import os
import json


async def main():
    robot_ip = "10.103.1.111" # Change this to your robot's IP address
    desk = Desk(robot_ip, platform="fr3")

    username = os.environ.get("PANDA_USERNAME") # Change this to your username
    password = os.environ.get("PANDA_PASSWORD") # Change this to your password

    await desk.login(username=username, password=password)
    await desk.take_control(force=True)
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

    # async def print_robot_state():
    #     async with desk.robot_states() as generator:
    #         async for s in generator:
    #             # Returns cartesian pose (16), estimated forces (6), estimated torques (7), and joint angles (7).
    #             s = json.dumps(s, indent=4)
    #             print(s)

    # async def print_safety_status():
    #     async with desk.safety_status() as generator:
    #         async for s in generator:
    #             s = json.dumps(s, indent=4)
    #             print(s)

    # async def print_general_system_status():
    #     async with desk.general_system_status() as generator:
    #         async for s in generator:
    #             s = json.dumps(s, indent=4)
    #             print(s)

    # async def print_system_status():
    #     async with desk.system_status() as generator:
    #         async for s in generator:
    #             s = json.dumps(s, indent=4)
    #             print(s)

    # async def print_button_events():
    #     async with desk.button_events() as generator:
    #         async for s in generator:
    #             s = json.dumps(s, indent=4)
    #             print(s)
    
    # await print_robot_state()
    
    # async with trio.open_nursery() as nursery:
        # nursery.start_soon(print_robot_state)
        # nursery.start_soon(print_safety_status)
        # nursery.start_soon(print_general_system_status)
        # nursery.start_soon(print_system_status)
        # nursery.start_soon(print_button_events)

if __name__ == "__main__":
    trio.run(main)