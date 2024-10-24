#!/usr/bin/env python3

import argparse
from typing import Sequence
from aloha.constants import (
    DT_DURATION,
    FOLLOWER_GRIPPER_JOINT_CLOSE,
    LEADER2FOLLOWER_JOINT_FN,
    LEADER_GRIPPER_CLOSE_THRESH,
    LEADER_GRIPPER_JOINT_MID,
    START_ARM_POSE,
)
from aloha.robot_utils import (
    enable_gravity_compensation,
    get_arm_gripper_positions,
    move_arms,
    move_grippers,
    torque_off,
    torque_on,
)
from interbotix_common_modules.common_robot.robot import (
    create_interbotix_global_node,
    get_interbotix_global_node,
    robot_shutdown,
    robot_startup,
)
from interbotix_xs_modules.xs_robot.arm import InterbotixManipulatorXS
from interbotix_xs_msgs.msg import JointSingleCommand
import rclpy
import yaml


def load_yaml_file(yaml_path="../config/aloha_static.yaml"):
    # Function to read and parse the YAML file
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)
    
def opening_ceremony(robots: dict) -> None:
    """Move all leader-follower pairs of robots to a starting pose for demonstration."""
    # Separate leader and follower robots
    leader_bots = {name: bot for name, bot in robots.items() if 'leader' in name}
    follower_bots = {name: bot for name, bot in robots.items() if 'follower' in name}

    # Define pairs for leader and follower bots based on naming convention
    pairs = []

    # Check for common suffixes (_left, _right, _solo)
    for leader_name, leader_bot in leader_bots.items():
        for suffix in ['_left', '_right', '_solo']:
            if leader_name.endswith(suffix):
                follower_name = f"follower{suffix}"
                if follower_name in follower_bots:
                    pairs.append((leader_bot, follower_bots[follower_name]))
                break
        else:
            # Handle case if no specific suffix is found, just pair remaining leader and follower
            if follower_bots:
                follower_bot_name, follower_bot = follower_bots.popitem()
                pairs.append((leader_bot, follower_bot))

    # Ensure that we have at least one pair of leader and follower bots
    if not pairs:
        raise ValueError("No valid leader-follower pairs found in the robot dictionary.")

    # Iterate through the leader-follower pairs
    for leader_bot, follower_bot in pairs:
        # Reboot gripper motors and set operating modes for all motors
        follower_bot.core.robot_reboot_motors('single', 'gripper', True)
        follower_bot.core.robot_set_operating_modes('group', 'arm', 'position')
        follower_bot.core.robot_set_operating_modes('single', 'gripper', 'current_based_position')
        leader_bot.core.robot_set_operating_modes('group', 'arm', 'position')
        leader_bot.core.robot_set_operating_modes('single', 'gripper', 'position')
        follower_bot.core.robot_set_motor_registers('single', 'gripper', 'current_limit', 300)

        # Turn on torque for both the leader and follower
        torque_on(follower_bot)
        torque_on(leader_bot)

        # Move arms to starting position
        start_arm_qpos = START_ARM_POSE[:6]
        move_arms(
            [leader_bot, follower_bot],
            [start_arm_qpos] * 2,
            moving_time=4.0,
        )

        # Move grippers to starting position
        move_grippers(
            [leader_bot, follower_bot],
            [LEADER_GRIPPER_JOINT_MID, FOLLOWER_GRIPPER_JOINT_CLOSE],
            moving_time=0.5
        )


def press_to_start(robots: dict, gravity_compensation: bool) -> None:
    """Wait for the user to close the grippers on all leader robots to start."""
    
    # Extract leader bots from the robots dictionary
    leader_bots = {name: bot for name, bot in robots.items() if 'leader' in name}

    # Disable torque for the gripper joint of each leader bot to allow user movement
    for leader_name, leader_bot in leader_bots.items():
        leader_bot.core.robot_torque_enable('single', 'gripper', False)

    print('Close the grippers to start')

    # Wait for the user to close the grippers of all leader robots
    pressed = False
    while rclpy.ok() and not pressed:
        pressed = all(
            get_arm_gripper_positions(leader_bot) < LEADER_GRIPPER_CLOSE_THRESH
            for leader_bot in leader_bots.values()
        )
        get_interbotix_global_node().get_clock().sleep_for(DT_DURATION)

    # Enable gravity compensation or turn off torque based on the parameter
    for leader_name, leader_bot in leader_bots.items():
        if gravity_compensation:
            enable_gravity_compensation(leader_bot)
        else:
            torque_off(leader_bot)

    print('Started!')


def main(args: dict) -> None:
    gravity_compensation = args.get('gravity_compensation', False)

    node = create_interbotix_global_node('aloha')

    config = load_yaml_file()
    # Dictionary to hold the dynamically created robot instances
    robots = {}

    # Create leader arms
    for leader in config.get('leader_arms', []):
        print(leader['name'])
        robot_instance = InterbotixManipulatorXS(
            robot_model=leader['model'],
            robot_name=leader['name'],
            node=node,
            iterative_update_fk=False,
        )
        robots[leader['name']] = robot_instance

    # Create follower arms
    for follower in config.get('follower_arms', []):
        print(follower['name'])
        robot_instance = InterbotixManipulatorXS(
            robot_model=follower['model'],
            robot_name=follower['name'],
            node=node,
            iterative_update_fk=False,
        )
        robots[follower['name']] = robot_instance

    robot_startup(node)
    opening_ceremony(
        robots
    )
    
    press_to_start(robots, gravity_compensation)
    # Teleoperation loop
    # Define gripper command objects for each follower
    gripper_commands = {
        follower_name: JointSingleCommand(name='gripper') for follower_name in robots if 'follower' in follower_name
    }
    while rclpy.ok():
        # Iterate through each leader-follower pair
        for leader_name, leader_bot in robots.items():
            if 'leader' in leader_name:
                # Determine the corresponding follower based on suffix
                suffix = leader_name.replace('leader', '')
                follower_name = f'follower{suffix}'
                follower_bot = robots.get(follower_name)

                if follower_bot:
                    # Sync arm joint positions
                    leader_state_joints = leader_bot.core.joint_states.position[:6]
                    follower_bot.arm.set_joint_positions(leader_state_joints, blocking=False)

                    # Sync gripper positions
                    gripper_command = gripper_commands[follower_name]
                    gripper_command.cmd = LEADER2FOLLOWER_JOINT_FN(
                        leader_bot.core.joint_states.position[6]
                    )
                    follower_bot.gripper.core.pub_single.publish(gripper_command)

        # Sleep for the DT duration
        get_interbotix_global_node().get_clock().sleep_for(DT_DURATION)

    robot_shutdown(node)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-g', '--gravity_compensation',
        action='store_true',
        help='If set, gravity compensation will be enabled for the leader robots when teleop starts.',
    )
    main(vars(parser.parse_args()))
