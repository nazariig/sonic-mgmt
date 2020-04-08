import os
import pytest
import time
import sys
import json
import yaml
from common import reboot
from common.plugins.loganalyzer.loganalyzer import LogAnalyzer, LogAnalyzerError
from datetime import datetime
from check_critical_services import check_critical_services
from check_daemon_status import check_pmon_daemon_status
import re
from common.utilities import wait_until
import logging

FW_UTIL_DATA = {}
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
BINARIES_DIR = os.path.join(BASE_DIR, 'binaries')
TMP_DIR = os.path.basename('tmp')
SUCCESS_CODE = 0
FAILURE_CODE = -1

PLATFORM_COMP_PATH_TEMPLATE = '/usr/share/sonic/device/{}/platform_components.json'

FW_INSTALL_SUCCESS_LOG = "*.Firmware install ended * status=success*."
UNVALID_NAME_LOG = '.*Invalid value for "<component_name>"*.'
UNVALID_PATH_LOG = '.*Error: Invalid value for "fw_path"*.'
UNVALID_URL_LOG = '.*Error: Did not receive a response from remote machine. Aborting...*.'
INVALID_PLATFORM_SCHEMA_LOG = '.*Error: Failed to parse "platform_components.json": invalid platform schema*.'
INVALID_CHASSIS_SCHEMA_LOG = '.*Error: Failed to parse "platform_components.json": invalid chassis schema*.'
INVALID_COMPONENT_SCHEMA_LOG = '.*Error: Failed to parse "platform_components.json": invalid component schema*.'

logger = logging.getLogger(__name__)


class FwComponent(object):

    def get_version(self, dut, files_path, fw_data):
        raise NotImplemented

    def update_fw(self, request):
        raise NotImplemented

    def check_version(self, version_to_install, comp_data):
        raise NotImplemented

    def get_component_name(self):
        raise NotImplemented

    def process_versions(self, dut, location):
        """
        process latest/other versions of arbitrary picked component
        """
        comp_versions = self.get_version(dut, files_path=location, fw_data=get_output_data(dut))

        if comp_versions['latest_installed']:
            is_latest_installed = True
            current_fw_path = comp_versions['latest_path']
            fw_path_to_install = comp_versions['other_path']
            version_to_install = comp_versions['other_version']
            previous_ver = comp_versions['latest_version']

        else:
            is_latest_installed = False
            current_fw_path = comp_versions['other_path']
            fw_path_to_install = comp_versions['latest_path']
            version_to_install = comp_versions['latest_version']
            previous_ver = comp_versions['other_version']

        return {
            'is_latest_installed': is_latest_installed,
            'current_component': self.get_component_name(),
            'current_fw_path': current_fw_path,
            'path_to_install': fw_path_to_install,
            'version_to_install': version_to_install,
            'previous_ver': previous_ver
        }


class BiosComponent(FwComponent):

    def __init__(self, component_name):
        self.__name = component_name

    def parse_version(self, files_path, file_name):
        fw_path = os.path.join(files_path, file_name)
        release_path = os.path.realpath(fw_path)
        ver = os.path.dirname(release_path).rsplit('/', 1)[1]
        ver = ver[::-1].replace('x', '0', 1)[::-1]
        for file_name in os.listdir(fw_path):
            if file_name.endswith('.rom'):
                fw_path = os.path.join(fw_path, file_name)
                break
        return fw_path, ver

    def get_version(self, dut, files_path, fw_data):
        files_path = os.path.join(files_path, 'bios')
        versions = {}
        platform_type = dut.facts['platform']
        is_latest = False
        latest_ver = ''
        other_ver = ''
        latest = platform_type + '_latest'
        latest_fw_path = ""
        other = platform_type + '_other'
        other_fw_path = ""
        for file_name in os.listdir(files_path):
            if file_name.startswith(latest):
                latest_fw_path, latest_ver = self.parse_version(files_path, file_name)
                if fw_data['BIOS']['version'].startswith(latest_ver):
                    is_latest = True
            elif file_name.startswith(other):
                other_fw_path, other_ver = self.parse_version(files_path, file_name)
        versions = {
                'latest_version': latest_ver,
                'latest_path': latest_fw_path,
                'latest_installed': is_latest,
                'other_version': other_ver,
                'other_path': other_fw_path,
        }
        logger.info(
            "{} parsed versions:\n{}".format(
                self.get_component_name(),
                json.dumps(versions, indent=4)
            )
        )

        return versions

    def update_fw(self, request):
        """
        perform cold reboot to make bios installation finished.
        :param request
        :param dut - DUT
        """
        testbed_device = request.getfixturevalue("testbed_devices")
        localhost = testbed_device['localhost']
        dut = testbed_device['dut']
        reboot_ctrl_dict = {
            'command': 'reboot',
            'timeout': 600,
            'cause': 'reboot',
            'test_reboot_cause_only': False
        }

        logging.info("Run cold reboot on DUT")
        reboot_cmd = reboot_ctrl_dict['command']
        reboot_task, reboot_res = dut.command(reboot_cmd, module_ignore_errors=True, module_async=True)
        logging.info("Wait for DUT to go down")
        res = localhost.wait_for(host=dut.hostname, port=22, state="stopped", timeout=180, module_ignore_errors=True)
        if "failed" in res:
            try:
                logging.error("Wait for switch down failed, try to kill any possible stuck reboot task")
                pid = dut.command("pgrep -f '%s'" % reboot_cmd)['stdout']
                dut.command("kill -9 %s" % pid)
                reboot_task.terminate()
                logging.error("Result of command '%s': " + str(reboot_res.get(timeout=0)))
            except Exception as e:
                logging.error("Exception raised while cleanup reboot task and get result: " + repr(e))

        # wait for dut to go up
        logging.info("Wait for DUT to come back")
        localhost.wait_for(host=dut.hostname, port=22, state="started", delay=10, timeout=300)

        logging.info("Wait until system is stable")
        wait_until(300, 30, dut.critical_services_fully_started)

        # ensure system init is done
        time.sleep(30)

    def check_version(self, version_to_install, comp_data):
        """
        check if bios version was updated as expected.
        """
        if comp_data['version'].startswith(version_to_install):
            return SUCCESS_CODE

        return FAILURE_CODE

    def get_component_name(self):
        return self.__name


class CpldComponent(FwComponent):

    def __init__(self, component_name):
        self.__name = component_name

    def get_part_number(self, platform_type, files_path):
        cpld_pn = None

        conf_path = os.path.join(files_path, "{}/cpld_name_to_pn.yml".format(platform_type))
        with open(conf_path, "r") as config:
            cpld_name_to_pn_dict = yaml.safe_load(config)
            cpld_pn = cpld_name_to_pn_dict[self.__name]

        return cpld_pn

    def parse_version(self, platform_type, files_path, file_name, fw_data):
        fw_path = os.path.join(files_path, file_name)
        real_fw_path = os.path.realpath(fw_path)
        basename = os.path.basename(real_fw_path)
        name = os.path.splitext(basename)[0]
        rev = name.upper()

        # get CPLD part number
        cpld_pn = self.get_part_number(platform_type, files_path)

        if cpld_pn not in rev:
            raise RuntimeError(
                "Part number is not found: cpld={}, pn={}".format(
                    self.__name,
                    cpld_pn
                )
            )

        # parse CPLD version
        cpld_ver = rev.split(cpld_pn)[1]
        cpld_ver = cpld_ver[1:].split('_')[0]
        cpld_ver_major = cpld_ver[:5]
        cpld_ver_minor = cpld_ver[5:]

        # parse component version
        comp_pn = fw_data[self.__name]['version'].split('_')[0]
        comp_ver = fw_data[self.__name]['version'].split('_')[1]
        comp_ver_major = comp_ver[:5]
        comp_ver_minor = comp_ver[5:]

        # TODO: Provide better way for handling minor version support
        if int(comp_ver_minor) != 0:
            parsed_ver = "{}_{}{}".format(comp_pn, cpld_ver_major, cpld_ver_minor)
        else:
            parsed_ver = "{}_{}00".format(comp_pn, cpld_ver_major)

        return parsed_ver, cpld_ver_major == comp_ver_major

    def get_version(self, dut, files_path, fw_data):
        # currently taken from revisions but without known order
        files_path = os.path.join(files_path, 'cpld')
        versions = {}
        platform_type = dut.facts['platform']
        is_latest = False
        latest_ver = ''
        other_ver = ''
        latest = platform_type + '_latest'
        latest_fw_path = ""
        other = platform_type + '_other'
        other_fw_path = ""

        for file_name in os.listdir(files_path):
            if file_name.startswith(latest):
                latest_ver, is_latest = self.parse_version(platform_type, files_path, file_name, fw_data)
                latest_fw_path = os.path.join(files_path, file_name)
            if file_name.startswith(other):
                other_ver, is_other = self.parse_version(platform_type, files_path, file_name, fw_data)
                other_fw_path = os.path.join(files_path, file_name)

        versions = {
            'latest_version': latest_ver,
            'latest_path': latest_fw_path,
            'latest_installed': is_latest,
            'other_version': other_ver,
            'other_path': other_fw_path
        }
        logger.info(
            "{} parsed versions:\n{}".format(
                self.get_component_name(),
                json.dumps(versions, indent=4)
            )
        )

        return versions

    def update_fw(self, request):
        """
        performs 30 sec power cycle off to finish cpld installation.
        """
        testbed_devices = request.getfixturevalue("testbed_devices")
        localhost = testbed_devices['localhost']
        dut = testbed_devices['dut']

        cmd_num_psu = "sudo psuutil numpsus"
        logging.info("Check how much PSUs DUT has")
        psu_num_out = dut.command(cmd_num_psu)
        psu_num = 0
        try:
            psu_num = int(psu_num_out['stdout'])
        except:
            assert False, "Unable to get the number of PSUs using command '%s'" % cmd_num_psu

        logging.info("Create PSU controller for testing")
        psu_control = request.getfixturevalue("psu_controller")
        if psu_control is None:
            pytest.fail("No PSU controller for %s, skip rest of the testing in this case" % dut.hostname)
        all_psu_status = psu_control.get_psu_status()
        if all_psu_status:
            for psu in all_psu_status:
                if psu['psu_on']:
                    psu_control.turn_off_psu(psu['psu_id'])
                    time.sleep(5)

            # perform 30 seconds timeout
            time.sleep(30)

            all_psu_status = psu_control.get_psu_status()
            if all_psu_status:
                # turn on all psu
                for psu in all_psu_status:
                    if not psu['psu_on']:
                        psu_control.turn_on_psu(psu['psu_id'])
                        time.sleep(5)

        # wait for dut to go up
        logging.info("Wait for DUT to come back")
        localhost.wait_for(host=dut.hostname, port=22, state="started", delay=10, timeout=300)

        logging.info("Wait until system is stable")
        wait_until(300, 30, dut.critical_services_fully_started)

        # ensure system init is done
        time.sleep(30)

    def check_version(self, version_to_install, comp_data):
        """
        Check if there is version in comp data (TODO: when possible, check the cpld version)
        """
        if comp_data['version'].startswith(version_to_install):
            return SUCCESS_CODE

        return FAILURE_CODE

    def get_component_name(self):
        return self.__name


def fw_status(dut):
    """
    Gets fwutil show status command output
    :param dut: DUT
    """
    result = dut.command("fwutil show status")
    if result['rc'] != SUCCESS_CODE:
        pytest.fail("Could not execute command 'fwutil show status'")
    return result['stdout']


def get_output_data(dut):
    """
    Parse output of 'fwutil show status'
    and return the data
    :param dut: DUT
    """
    num_spaces = 2
    status_output = fw_status(dut)
    output_data = {}
    separators = re.split(r'\s{2,}', status_output.splitlines()[1])  # get separators
    output_lines = status_output.splitlines()[2:]

    for line in output_lines:
        data = []
        start = 0

        for sep in separators:
            curr_len = len(sep)
            data.append(line[start:start+curr_len].strip())
            start += curr_len + num_spaces

        component = data[2]
        output_data[component] = {
            'version': data[3],
            'desc': data[4]
        }

    return output_data


def execute_update_cmd(request, cmd, component, version_to_install, component_object, expected_log):
    """
    execute the recievd command on DUT, perform the final update, and check validation.
    """
    testbed_devices = request.getfixturevalue("testbed_devices")
    dut = testbed_devices['dut']

    loganalyzer = LogAnalyzer(ansible_host=dut, marker_prefix='acl')
    loganalyzer.load_common_config()
    try:
        loganalyzer.except_regex = [expected_log]
        with loganalyzer:
            result = dut.command(cmd)
    except LogAnalyzerError as err:
        raise err

    if result['rc'] != SUCCESS_CODE:
        pytest.fail("Update failed: msg={}".format(result['stderr']))

    # complete fw update - cold reboot if BIOS, power cycle with 30 sec timeout if CPLD
    component_object.update_fw(request)

    # check output of show command
    fw_data = get_output_data(dut)
    comp_data = fw_data[component]
    if not comp_data['version']:
        pytest.fail("Installation didn't work. Aborting!")

    if component_object.check_version(version_to_install, comp_data) != SUCCESS_CODE:
        pytest.fail(
            "Version check failed: current({}) != expected({})".format(
                comp_data['version'],
                version_to_install
            )
        )


def execute_wrong_command(dut, cmd, expected_log):
    """
    execute wrong command and verify that error occures.
    """
    result = dut.command(cmd, module_ignore_errors=True)
    if result['rc'] == SUCCESS_CODE:
        pytest.fail("Expected error code!")

    if not result['stderr'].find(expected_log):
        if not result['stdout'].find(expected_log):
            pytest.fail("Expected logs didn't occure!")


def generate_components_file(dut, components_list, current_comp, path_to_install, version_to_install):
    """
    generate new platform_components.json file
    """
    fw_data = get_output_data(dut)
    platform_type = dut.facts['platform']
    json_data = {}
    json_data['chassis'] = {}
    json_data['chassis'][platform_type] = {}
    json_data['chassis'][platform_type]['component'] = {}

    for comp in components_list:
        json_data['chassis'][platform_type]['component'][comp] = {}
        if current_comp == comp:
            json_data['chassis'][platform_type]['component'][comp]['firmware'] = path_to_install
            json_data['chassis'][platform_type]['component'][comp]['version'] = version_to_install
            json_data['chassis'][platform_type]['component'][comp]['info'] = fw_data[comp]['desc']

    with open(os.path.join(BASE_DIR, "tmp_platform_components.json"), "w") as comp_file:
        json.dump(json_data, comp_file, indent=4)

    dst = "/usr/share/sonic/device/{}/platform_components.json".format(platform_type)
    dut.copy(src=os.path.join(BASE_DIR, "tmp_platform_components.json"), dest=dst)


def update(request, cmd, current_comp, path_to_install, version_to_install, comp_path, component_object):
    """"
    Perform update command
    """
    testbed_devices = request.getfixturevalue("testbed_devices")
    dut = testbed_devices['dut']

    dut.copy(src=path_to_install, dest=comp_path)

    try:
        execute_update_cmd(
            request,
            cmd,
            current_comp,
            version_to_install,
            component_object,
            expected_log=FW_INSTALL_SUCCESS_LOG
        )
    finally:
        dut.command("rm -rf {}".format(comp_path))


def update_from_current_img(request, get_fw_path, components_list, component_object):
    """
    update from current image test case
    """
    testbed_devices = request.getfixturevalue("testbed_devices")
    dut = testbed_devices['dut']

    update_cmd = "fwutil update -y --image=current"
    current_component = get_fw_path['current_component']
    comp_path = os.path.join("/tmp", current_component)
    dut.command("mkdir -p {}".format(comp_path))
    comp_path = os.path.join(comp_path, os.path.basename(get_fw_path['current_fw_path']))

    try:
        generate_components_file(
            dut,
            components_list,
            current_comp=current_component,
            path_to_install=comp_path,
            version_to_install=get_fw_path['previous_ver']
        )
        update(
            request,
            update_cmd,
            current_comp=current_component,
            path_to_install=get_fw_path['current_fw_path'],
            version_to_install=get_fw_path['previous_ver'],
            comp_path=comp_path,
            component_object=component_object
        )
    finally:
        dut.command("rm -rf {}".format(comp_path))


def get_image_info(dut):
    """
    @summary: Parse image info in output of command 'sonic_installer list'
    @param module: The AnsibleModule object
    @return: Return parsed image info in dict
    """
    cmd = "sudo sonic_installer list"
    result = dut.command(cmd)
    if result['rc'] != 0:
        pytest.fail('Failed to run %s, rc=%s, stdout=%s, stderr=%s' % (cmd, result['rc'], result['stdout'], result['stderr']))
    stdout = result['stdout']
    try:
        image_info = {}
        image_list_line = False
        for line in stdout.splitlines():
            if not image_list_line:
                if 'Current: ' in line:
                    image_info['current'] = line.split('Current: ')[1]
                if 'Next: ' in line:
                    image_info['next'] = line.split('Next: ')[1]
                if 'Available:' in line:
                    image_list_line = True
                    image_info['available'] = []
                    continue
            else:
                image_info['available'].append(line)
        return image_info
    except Exception as e:
        pytest.fail('Failed to parse image info from output of "%s", err=%s' % (cmd, str(e)))

    return None


def set_default_boot(request, dut):
    """
    Set current image as default.
    """
    image_info = get_image_info(dut)
    current_image = image_info['current']

    result = dut.command("sonic_installer set_default {}".format(current_image))
    if result['rc'] != SUCCESS_CODE:
        pytest.fail("Could not set default image {}. Aborting!".format(current_image))


def set_next_boot(request, dut):
    """
    Set other available image as next.
    If there is no other available image, get it from user arguments.
    """
    image_info = get_image_info(dut)
    next_img = image_info['next']
    if next_img == image_info['current']:
        for img in image_info['available']:
            if img != image_info['current']:
                next_img = img
                break
    if next_img == image_info['current']:
        try:
            second_image_path = request.config.getoption("--second_image_path")
            next_img = os.path.basename(second_image_path)
            dut.copy(src=second_image_path, dest='/home/admin')
            result = dut.command("sonic_installer install -y ./{}".format(next_img))
            if result['rc'] != SUCCESS_CODE:
                pytest.fail("Could not install image {}. Aborting!".format(next_img))
        except Exception as e:
            pytest.fail("Not enough images for this test. Aborting!")

    result = dut.command("sonic_installer set_next_boot {}".format(next_img))
    if result['rc'] != SUCCESS_CODE:
        pytest.fail("Could not set image {} as next boot. Aborting!".format(next_img))


def update_from_next_img(request, get_fw_path, components_list, component_object):
    """
    update from next image test case.
    """
    testbed_devices = request.getfixturevalue("testbed_devices")
    dut = testbed_devices['dut']

    set_next_boot(request, dut)

    next_img_cmd = "fwutil update -y --image=next"
    update_code = execute_update_cmd(
        request,
        cmd=next_img_cmd,
        component=get_fw_path['current_component'],
        version_to_install=get_fw_path['version_to_install'],
        component_object=component_object,
        expected_log=FW_INSTALL_SUCCESS_LOG
    )


def generate_invalid_structure_file(dut, components_list, chassis, platform_type, is_valid_comp_structure):
    """
    Generate invlid platform_components.json file - for negative test cases.
    """
    fw_data = get_output_data(dut)
    json_data = {}
    json_data[chassis] = {}
    json_data[chassis][platform_type] = {}
    json_data[chassis][platform_type]['component'] = {}

    for comp in components_list:
        json_data[chassis][platform_type]['component'][comp] = {}
        json_data[chassis][platform_type]['component'][comp]['firmware'] = 'path/to/install'
        if is_valid_comp_structure is False:
            json_data[chassis][platform_type]['component'][comp]['version'] = {}
            json_data[chassis][platform_type]['component'][comp]['version']['version'] = 'version/to/install'
        else:
            json_data[chassis][platform_type]['component'][comp]['version'] = 'version/to/install'
        json_data[chassis][platform_type]['component'][comp]['info'] = 'description'

    with open(os.path.join(BASE_DIR, "tmp_platform_components.json"), "w") as comp_file:
        json.dump(json_data, comp_file)

    dst = "/usr/share/sonic/device/{}/platform_components.json".format(platform_type)
    dut.copy(src=os.path.join(BASE_DIR, "tmp_platform_components.json"), dest=dst)


def reboot_to_image(request, image_type):
    """
    set the recieved image as default and reboot
    """
    testbed_devices = request.getfixturevalue("testbed_devices")
    dut = testbed_devices['dut']
    localhost = testbed_devices['localhost']

    # move to next image
    result = dut.command("sonic_installer set_default {}".format(image_type))
    if result['rc'] != SUCCESS_CODE:
        pytest.fail("Could not reboot the {} image".format(image_type))

    # reboot
    logging.info("Reboot the DUT to load image")
    reboot_task, reboot_res = dut.command("reboot", module_async=True)
    logging.info("Wait for DUT to go down")
    try:
        localhost.wait_for(host=dut.hostname, port=22, state="stopped", delay=10, timeout=300)
    except Exception as e:
        logging.error("DUT did not go down, exception: " + repr(e))
        if reboot_task.is_alive():
            logging.error("Rebooting is not completed")
            reboot_task.terminate()
            logging.error("reboot result %s" % str(reboot_res.get()))

    logging.info("Wait for DUT to come back")
    localhost.wait_for(host=dut.hostname, port=22, state="started", delay=10, timeout=300)

    logging.info("Wait until system is stable")
    wait_until(300, 30, dut.critical_services_fully_started)

    # ensure system init is done
    time.sleep(30)

    new_image_info = get_image_info(dut)
    if new_image_info['current'] != image_type:
        pytest.fail("Rebooting to {} image failed".format(image_type))
