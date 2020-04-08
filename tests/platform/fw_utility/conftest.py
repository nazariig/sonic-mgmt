import os
import pytest
from fwutil_helper import *
import random

BASE_DIR = os.path.dirname(os.path.realpath(__file__))


@pytest.fixture(scope='module')
def get_fw_path(request, testbed_devices, components_list, component_object):
    """
    fixture that returns fw paths.
    :param request: request for binaries path entered by the user.
    :param testbed_devices
    :param components_list: list of components
    """
    dut = testbed_devices['dut']

    binaries_path = request.config.getoption("--binaries_path")
    if not binaries_path:
        pytest.fail("Missing arguments: --binaries_path")

    yield component_object.process_versions(dut, binaries_path)


@pytest.fixture(scope='module')
def skip_if_latest_installed(get_fw_path):
    if get_fw_path['is_latest_installed']:
        pytest.skip(
            "Latest {} firmware is already installed".format(
                get_fw_path['current_component']
            )
        )


@pytest.fixture(scope='module')
def components_list(request, testbed_devices):
    """
    fixture that returns the components list
    according to the given config file.
    :param request
    :param testbed_devices: testbed devices
    """
    dut = testbed_devices['dut']
    config_file = request.config.getoption("--config_file")
    # config file contains platform string identifier and components separated by ','.
    # e.g.: x86_64-mlnx_msn2010-r0: BIOS,CPLD
    conf_path = os.path.join(BASE_DIR, config_file)
    with open(conf_path, "r") as config:
        platforms_dict = yaml.safe_load(config)
        platform_type = dut.facts['platform']
        components = platforms_dict[platform_type]

    yield components.split(",")


@pytest.fixture(scope='function')
def backup_platform_file(testbed_devices):
    """
    backup the original platform_components.json file
    """
    dut = testbed_devices['dut']

    platform_type = dut.facts['platform']
    platform_comp_path = '/usr/share/sonic/device/' + platform_type + '/platform_components.json'
    backup_path = os.path.join(BASE_DIR, "platform_component_backup.json")
    res = dut.fetch(src=platform_comp_path, dest=backup_path, flat="yes")

    yield

    dut.copy(src=backup_path, dest=platform_comp_path)


@pytest.fixture(scope='function')
def setup_images(request, testbed_devices, get_fw_path, components_list):
    """"
    setup part of 'update from next image test' case.
    backup both images files and generate new json files.
    """
    dut = testbed_devices['dut']

    set_default_boot(request, dut)
    set_next_boot(request, dut)
    image_info = get_image_info(dut)
    current_image = image_info['current']
    next_image = image_info['next']

    platform_type = dut.facts['platform']
    platform_comp_path = PLATFORM_COMP_PATH_TEMPLATE.format(platform_type)

    # backup current image platform file
    current_backup_path = os.path.join(BASE_DIR, "current_platform_component_backup.json")
    dut.fetch(src=platform_comp_path, dest=current_backup_path, flat="yes")

    # reboot to next image
    reboot_to_image(request, image_type=next_image)

    # backup next-image platform file
    next_backup_path = os.path.join(BASE_DIR, "next_platform_component_backup.json")
    dut.fetch(src=platform_comp_path, dest=next_backup_path, flat="yes")

    # generate component file for the next image
    current_component = get_fw_path['current_component']
    comp_path = os.path.join("/home/admin", current_component)
    dut.command("mkdir -p {}".format(comp_path))
    comp_path = os.path.join(comp_path, os.path.basename(get_fw_path['path_to_install']))
    generate_components_file(
        dut,
        components_list,
        current_component,
        comp_path,
        get_fw_path['version_to_install']
    )
    # copy fw to dut (next image)
    dut.copy(src=get_fw_path['path_to_install'], dest=comp_path)

    # reboot to first image
    reboot_to_image(request, image_type=current_image)

    yield

    # teardown
    new_image_info = get_image_info(dut)
    if new_image_info['current'] == next_image:
        dut.command("rm -rf {}".format(comp_path))
        # restore json file
        dut.copy(src=next_backup_path, dest=PLATFORM_COMP_PATH_TEMPLATE.format(platform_type))
        reboot_to_image(request, image_type=current_image)
        dut.copy(src=current_backup_path, dest=PLATFORM_COMP_PATH_TEMPLATE.format(platform_type))
    else:
        dut.copy(src=current_backup_path, dest=PLATFORM_COMP_PATH_TEMPLATE.format(platform_type))
        reboot_to_image(request, image_type=next_image)
        dut.copy(src=next_backup_path, dest=PLATFORM_COMP_PATH_TEMPLATE.format(platform_type))
        reboot_to_image(request, image_type=current_image)


@pytest.fixture(scope='module')
def component_object(components_list):
    current_comp = random.choice(components_list)

    pattern = re.compile('^[A-Za-z]+')
    result = pattern.search(current_comp.capitalize())
    if not result:
        pytes.fail("Failed to detect component type: name={}".format(current_comp))

    yield globals()[result.group(0).lower().capitalize() + 'Component'](current_comp)
