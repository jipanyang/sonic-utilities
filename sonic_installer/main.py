#! /usr/bin/python -u

import os
import re
import signal
import stat
import sys
import time
import click
import urllib
import subprocess
from swsssdk import ConfigDBConnector
from swsssdk import SonicV2Connector
import collections

HOST_PATH = '/host'
IMAGE_PREFIX = 'SONiC-OS-'
IMAGE_DIR_PREFIX = 'image-'
ONIE_DEFAULT_IMAGE_PATH = '/tmp/sonic_image'
ABOOT_DEFAULT_IMAGE_PATH = '/tmp/sonic_image.swi'
IMAGE_TYPE_ABOOT = 'aboot'
IMAGE_TYPE_ONIE = 'onie'
ABOOT_BOOT_CONFIG = '/boot-config'

#
# Helper functions
#

# Needed to prevent "broken pipe" error messages when piping
# output of multiple commands using subprocess.Popen()
def default_sigpipe():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def reporthook(count, block_size, total_size):
    global start_time, last_time
    cur_time = int(time.time())
    if count == 0:
        start_time = cur_time
        last_time = cur_time
        return

    if cur_time == last_time:
        return

    last_time = cur_time

    duration = cur_time - start_time
    progress_size = int(count * block_size)
    speed = int(progress_size / (1024 * duration))
    percent = int(count * block_size * 100 / total_size)
    time_left = (total_size - progress_size) / speed / 1024
    sys.stdout.write("\r...%d%%, %d MB, %d KB/s, %d seconds left...   " %
                                  (percent, progress_size / (1024 * 1024), speed, time_left))
    sys.stdout.flush()

def get_image_type():
    cmdline = open('/proc/cmdline', 'r')
    if "Aboot=" in cmdline.read():
        return IMAGE_TYPE_ABOOT
    return IMAGE_TYPE_ONIE

def aboot_read_boot_config(path):
    config = collections.OrderedDict()
    with open(path) as f:
        for line in f.readlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            config[key] = value
    return config

def aboot_write_boot_config(path, config):
    with open(path, 'w') as f:
        f.write(''.join( '%s=%s\n' % (k, v) for k, v in config.items()))

def aboot_boot_config_set(**kwargs):
    path = kwargs.get('path', HOST_PATH + ABOOT_BOOT_CONFIG)
    config = aboot_read_boot_config(path)
    for key, value in kwargs.items():
        config[key] = value
    aboot_write_boot_config(path, config)

def aboot_image_path(image):
    image_dir = image.replace(IMAGE_PREFIX, IMAGE_DIR_PREFIX)
    return 'flash:%s/.sonic-boot.swi' % image_dir

# Run bash command and print output to stdout
def run_command(command):
    click.echo(click.style("Command: ", fg='cyan') + click.style(command, fg='green'))

    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)
    (out, err) = proc.communicate()

    click.echo(out)

    if proc.returncode != 0:
        sys.exit(proc.returncode)

# Returns list of installed images
def get_installed_images():
    images = []
    if get_image_type() == IMAGE_TYPE_ABOOT:
        for filename in os.listdir(HOST_PATH):
            if filename.startswith(IMAGE_DIR_PREFIX):
                images.append(filename.replace(IMAGE_DIR_PREFIX, IMAGE_PREFIX))
    else:
        config = open(HOST_PATH + '/grub/grub.cfg', 'r')
        for line in config:
            if line.startswith('menuentry'):
                image = line.split()[1].strip("'")
                if IMAGE_PREFIX in image:
                    images.append(image)
        config.close()
    return images

# Returns name of current image
def get_current_image():
    cmdline = open('/proc/cmdline', 'r')
    current = re.search("loop=(\S+)/fs.squashfs", cmdline.read()).group(1)
    cmdline.close()
    return current.replace(IMAGE_DIR_PREFIX, IMAGE_PREFIX)

# Returns name of next boot image
def get_next_image():
    if get_image_type() == IMAGE_TYPE_ABOOT:
        config = open(HOST_PATH + ABOOT_BOOT_CONFIG, 'r')
        next_image = re.search("SWI=flash:(\S+)/", config.read()).group(1).replace(IMAGE_DIR_PREFIX, IMAGE_PREFIX)
        config.close()
    else:
        images = get_installed_images()
        grubenv = subprocess.check_output(["/usr/bin/grub-editenv", HOST_PATH + "/grub/grubenv", "list"])
        m = re.search("next_entry=(\d+)", grubenv)
        if m:
            next_image_index = int(m.group(1))
        else:
            m = re.search("saved_entry=(\d+)", grubenv)
            if m:
                next_image_index = int(m.group(1))
            else:
                next_image_index = 0
        next_image = images[next_image_index]
    return next_image

def remove_image(image):
    if get_image_type() == IMAGE_TYPE_ABOOT:
        nextimage = get_next_image()
        current = get_current_image()
        if image == nextimage:
            image_path = aboot_image_path(current)
            aboot_boot_config_set(SWI=image_path, SWI_DEFAULT=image_path)
            click.echo("Set next and default boot to current image %s" % current)

        image_dir = image.replace(IMAGE_PREFIX, IMAGE_DIR_PREFIX)
        click.echo('Removing image root filesystem...')
        subprocess.call(['rm','-rf', os.path.join(HOST_PATH, image_dir)])
        click.echo('Image removed')
    else:
        click.echo('Updating GRUB...')
        config = open(HOST_PATH + '/grub/grub.cfg', 'r')
        old_config = config.read()
        menuentry = re.search("menuentry '" + image + "[^}]*}", old_config).group()
        config.close()
        config = open(HOST_PATH + '/grub/grub.cfg', 'w')
        # remove menuentry of the image in grub.cfg
        config.write(old_config.replace(menuentry, ""))
        config.close()
        click.echo('Done')

        image_dir = image.replace(IMAGE_PREFIX, IMAGE_DIR_PREFIX)
        click.echo('Removing image root filesystem...')
        subprocess.call(['rm','-rf', HOST_PATH + '/' + image_dir])
        click.echo('Done')

        run_command('grub-set-default --boot-directory=' + HOST_PATH + ' 0')
        click.echo('Image removed')

# TODO: Embed tag name info into docker image meta data at build time,
# and extract tag name from docker image file.
def get_docker_tag_name(image):
    # Try to get tag name from label metadata
    cmd = "docker inspect --format '{{.ContainerConfig.Labels.Tag}}' " + image
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    (out, err) = proc.communicate()
    if proc.returncode != 0:
        return "unknown"
    tag = out.rstrip()
    if tag == "<no value>":
        return "unknown"
    return tag

# Function which validates whether a given URL specifies an existent file
# on a reachable remote machine. Will abort the current operation if not
def validate_url_or_abort(url):
    # Attempt to retrieve HTTP response code
    try:
        urlfile = urllib.urlopen(url)
        response_code = urlfile.getcode()
        urlfile.close()
    except IOError, err:
        response_code = None

    if not response_code:
        click.echo("Did not receive a response from remote machine. Aborting...")
        raise click.Abort()
    else:
        # Check for a 4xx response code which indicates a nonexistent URL
        if response_code / 100 == 4:
            click.echo("Image file not found on remote machine. Aborting...")
            raise click.Abort()

# Callback for confirmation prompt. Aborts if user enters "n"
def abort_if_false(ctx, param, value):
    if not value:
        ctx.abort()

def get_container_image_name(container_name):
    # example image: docker-lldp-sv2:latest
    cmd = "docker inspect --format '{{.Config.Image}}' " + container_name
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    (out, err) = proc.communicate()
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    image_latest = out.rstrip()

    # example image_name: docker-lldp-sv2
    cmd = "echo " + image_latest + " | cut -d ':' -f 1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    image_name = proc.stdout.read().rstrip()
    return image_name

def get_container_image_id(image_tag):
    # this is image_id for image with tag, like 'docker-teamd:latest'
    cmd = "docker images --format '{{.ID}}' " + image_tag
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    image_id = proc.stdout.read().rstrip()
    return image_id

def get_container_image_id_all(image_name):
    # All images id under the image name like 'docker-teamd'
    cmd = "docker images --format '{{.ID}}' " + image_name
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    image_id_all = proc.stdout.read()
    image_id_all = image_id_all.splitlines()
    image_id_all = set(image_id_all)
    return image_id_all

# Main entrypoint
@click.group()
def cli():
    """ SONiC image installation manager """
    if os.geteuid() != 0:
        exit("Root privileges required for this operation")


# Install image
@cli.command()
@click.option('-y', '--yes', is_flag=True, callback=abort_if_false,
        expose_value=False, prompt='New image will be installed, continue?')
@click.argument('url')
def install(url):
    """ Install image from local binary or URL"""
    cleanup_image = False
    if get_image_type() == IMAGE_TYPE_ABOOT:
        DEFAULT_IMAGE_PATH = ABOOT_DEFAULT_IMAGE_PATH
    else:
        DEFAULT_IMAGE_PATH = ONIE_DEFAULT_IMAGE_PATH

    if url.startswith('http://') or url.startswith('https://'):
        click.echo('Downloading image...')
        validate_url_or_abort(url)
        try:
            urllib.urlretrieve(url, DEFAULT_IMAGE_PATH, reporthook)
        except Exception, e:
            click.echo("Download error", e)
            raise click.Abort()
        image_path = DEFAULT_IMAGE_PATH
    else:
        image_path = os.path.join("./", url)

    # Verify that the local file exists and is a regular file
    # TODO: Verify the file is a *proper SONiC image file*
    if not os.path.isfile(image_path):
        click.echo("Image file '{}' does not exist or is not a regular file. Aborting...".format(image_path))
        raise click.Abort()

    if get_image_type() == IMAGE_TYPE_ABOOT:
        run_command("/usr/bin/unzip -od /tmp %s boot0" % image_path)
        run_command("swipath=%s target_path=/host sonic_upgrade=1 . /tmp/boot0" % image_path)
    else:
        os.chmod(image_path, stat.S_IXUSR)
        run_command(image_path)
        run_command('grub-set-default --boot-directory=' + HOST_PATH + ' 0')
    run_command("rm -rf /host/old_config")
    # copy directories and preserve original file structure, attributes and associated metadata
    run_command("cp -ar /etc/sonic /host/old_config")

    # sync filesystem, keep at last step.
    run_command("sync;sync;sync")
    run_command("sleep 3") # wait 3 seconds after sync
    click.echo('Done')


# List installed images
@cli.command()
def list():
    """ Print installed images """
    images = get_installed_images()
    curimage = get_current_image()
    nextimage = get_next_image()
    click.echo("Current: " + curimage)
    click.echo("Next: " + nextimage)
    click.echo("Available: ")
    for image in get_installed_images():
        click.echo(image)

# Set default image for boot
@cli.command()
@click.argument('image')
def set_default(image):
    """ Choose image to boot from by default """
    images = get_installed_images()
    if image not in images:
        click.echo('Image does not exist')
        sys.exit(1)
    if get_image_type() == IMAGE_TYPE_ABOOT:
        image_path = aboot_image_path(image)
        aboot_boot_config_set(SWI=image_path, SWI_DEFAULT=image_path)
    else:
        command = 'grub-set-default --boot-directory=' + HOST_PATH + ' ' + str(images.index(image))
        run_command(command)


# Set image for next boot
@cli.command()
@click.argument('image')
def set_next_boot(image):
    """ Choose image for next reboot (one time action) """
    images = get_installed_images()
    if image not in images:
        click.echo('Image does not exist')
        sys.exit(1)
    if get_image_type() == IMAGE_TYPE_ABOOT:
        image_path = aboot_image_path(image)
        aboot_boot_config_set(SWI=image_path)
    else:
        command = 'grub-reboot --boot-directory=' + HOST_PATH + ' ' + str(images.index(image))
        run_command(command)


# Uninstall image
@cli.command()
@click.option('-y', '--yes', is_flag=True, callback=abort_if_false,
        expose_value=False, prompt='Image will be removed, continue?')
@click.argument('image')
def remove(image):
    """ Uninstall image """
    images = get_installed_images()
    current = get_current_image()
    if image not in images:
        click.echo('Image does not exist')
        sys.exit(1)
    if image == current:
        click.echo('Cannot remove current image')
        sys.exit(1)

    remove_image(image)

# Retrieve version from binary image file and print to screen
@cli.command()
@click.argument('binary_image_path')
def binary_version(binary_image_path):
    """ Get version from local binary image file """
    if not os.path.isfile(binary_image_path):
        click.echo('Image file does not exist')
        sys.exit(1)

    # Attempt to determine whether this is an ONIE or Aboot image
    is_aboot = False

    with open(binary_image_path) as f:
        # Aboot file is a zip archive; check the start of the file for the zip magic number
        if f.read(4) == "\x50\x4b\x03\x04":
            is_aboot = True

    if is_aboot:
        p1 = subprocess.Popen(["unzip", "-p", binary_image_path, "boot0"], stdout=subprocess.PIPE, preexec_fn=default_sigpipe)
        p2 = subprocess.Popen(["grep", "-m 1", "^image_path"], stdin=p1.stdout, stdout=subprocess.PIPE, preexec_fn=default_sigpipe)
        p3 = subprocess.Popen(["sed", "-n", r"s/^image_path=\"\$target_path\/image-\(.*\)\"$/\1/p"], stdin=p2.stdout, stdout=subprocess.PIPE, preexec_fn=default_sigpipe)
    else:
        p1 = subprocess.Popen(["cat", "-v", binary_image_path], stdout=subprocess.PIPE, preexec_fn=default_sigpipe)
        p2 = subprocess.Popen(["grep", "-m 1", "^image_version"], stdin=p1.stdout, stdout=subprocess.PIPE, preexec_fn=default_sigpipe)
        p3 = subprocess.Popen(["sed", "-n", r"s/^image_version=\"\(.*\)\"$/\1/p"], stdin=p2.stdout, stdout=subprocess.PIPE, preexec_fn=default_sigpipe)

    stdout = p3.communicate()[0]
    p3.wait()
    version_num = stdout.rstrip('\n')

    if len(version_num) == 0:
        click.echo("File does not appear to be a vaild SONiC image file")
        sys.exit(1)

    click.echo(IMAGE_PREFIX + version_num)

# Remove installed images which are not current and next
@cli.command()
@click.option('-y', '--yes', is_flag=True, callback=abort_if_false,
        expose_value=False, prompt='Remove images which are not current and next, continue?')
def cleanup():
    """ Remove installed images which are not current and next """
    images = get_installed_images()
    curimage = get_current_image()
    nextimage = get_next_image()
    image_removed = 0
    for image in get_installed_images():
        if image != curimage and image != nextimage:
            click.echo("Removing image %s" % image)
            remove_image(image)
            image_removed += 1

    if image_removed == 0:
        click.echo("No image(s) to remove")

# Upgrade docker image
@cli.command()
@click.option('-y', '--yes', is_flag=True, callback=abort_if_false,
        expose_value=False, prompt='New docker image will be installed, continue?')
@click.option('--cleanup_image', is_flag=True, help="Clean up old docker image")
@click.option('--enforce_check', is_flag=True, help="Enforce pending task check for docker upgrade")
@click.option('--tag', type=str, help="Tag for the new docker image")
@click.option('--warm', is_flag=True, help="Perform warm upgrade")
@click.argument('container_name', metavar='<container_name>', required=True,
    type=click.Choice(["swss", "snmp", "lldp", "bgp", "pmon", "dhcp_relay", "telemetry", "teamd"]))
@click.argument('url')
def upgrade_docker(container_name, url, cleanup_image, enforce_check, tag, warm):
    """ Upgrade docker image from local binary or URL"""

    image_name = get_container_image_name(container_name)
    image_latest = image_name + ":latest"
    image_id_previous = get_container_image_id(image_latest)

    DEFAULT_IMAGE_PATH = os.path.join("/tmp/", image_name)
    if url.startswith('http://') or url.startswith('https://'):
        click.echo('Downloading image...')
        validate_url_or_abort(url)
        try:
            urllib.urlretrieve(url, DEFAULT_IMAGE_PATH, reporthook)
        except Exception, e:
            click.echo("Download error", e)
            raise click.Abort()
        image_path = DEFAULT_IMAGE_PATH
    else:
        image_path = os.path.join("./", url)

    # Verify that the local file exists and is a regular file
    # TODO: Verify the file is a *proper Docker image file*
    if not os.path.isfile(image_path):
        click.echo("Image file '{}' does not exist or is not a regular file. Aborting...".format(image_path))
        raise click.Abort()

    warm_configured = False
    # warm restart enable/disable config is put in stateDB, not persistent across cold reboot, not saved to config_DB.json file
    state_db = SonicV2Connector(host='127.0.0.1')
    state_db.connect(state_db.STATE_DB, False)
    TABLE_NAME_SEPARATOR = '|'
    prefix = 'WARM_RESTART_ENABLE_TABLE' + TABLE_NAME_SEPARATOR
    _hash = '{}{}'.format(prefix, container_name)
    if state_db.get(state_db.STATE_DB, _hash, "enable") == "true":
        warm_configured = True
    state_db.close(state_db.STATE_DB)

    if container_name == "swss" or container_name == "bgp" or container_name == "teamd":
        if warm_configured == False and warm:
           run_command("config warm_restart enable %s" % container_name)

    warm_app_name = ""
    # warm restart specific procssing for swss, bgp and teamd dockers.
    if warm_configured == True or warm:
        # make sure orchagent is in clean state if swss is to be upgraded
        if container_name == "swss":
            skipPendingTaskCheck = " -s"
            if enforce_check:
                skipPendingTaskCheck = ""

            cmd = "docker exec -i swss orchagent_restart_check -w 1000 " + skipPendingTaskCheck
            for i in range(1, 6):
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
                (out, err) = proc.communicate()
                if proc.returncode != 0:
                    if enforce_check:
                        click.echo("Orchagent is not in clean state, RESTARTCHECK failed {}".format(i))
                        if i == 5:
                            sys.exit(proc.returncode)
                    else:
                        click.echo("Orchagent is not in clean state, upgrading it anyway")
                        break
                else:
                    click.echo("Orchagent is in clean state and frozen for warm upgrade")
                    break
                run_command("sleep 1")
            # clean orchagent reconcilation state from last warm start if exists
            warm_app_name = "orchagent"
            cmd = "docker exec -i database redis-cli -n 6 hdel 'WARM_RESTART_TABLE|" + warm_app_name + "' state"
            run_command(cmd)

        elif container_name == "bgp":
            # Kill bgpd to restart the bgp graceful restart procedure
            click.echo("Stopping bgp ...")
            run_command("docker exec -i bgp pkill -9 zebra")
            run_command("docker exec -i bgp pkill -9 bgpd")
            run_command("sleep 2") # wait 2 seconds for bgp to settle down
            # clean bgp reconcilation state from last warm start if exists
            warm_app_name = "bgp"
            cmd = "docker exec -i database redis-cli -n 6 hdel 'WARM_RESTART_TABLE|" + warm_app_name + "' state"
            run_command(cmd)
            click.echo("Stopped  bgp ...")

        elif container_name == "teamd":
            click.echo("Stopping teamd ...")
            # Send USR1 signal to all teamd instances to stop them
            # It will prepare teamd for warm-reboot
            run_command("docker exec -i teamd pkill -USR1 teamd > /dev/null")
            run_command("sleep 2") # wait 2 seconds for teamd to settle down
            # clean teamsyncd reconcilation state from last warm start if exists
            warm_app_name = "teamsyncd"
            cmd = "docker exec -i database redis-cli -n 6 hdel 'WARM_RESTART_TABLE|" + warm_app_name + "' state"
            run_command(cmd)
            click.echo("Stopped  teamd ...")

    run_command("systemctl stop %s" % container_name)
    run_command("docker rm %s " % container_name)
    run_command("docker load < %s" % image_path)
    if tag == None:
        # example image: docker-lldp-sv2:latest
        tag = get_docker_tag_name(image_latest)
    run_command("docker tag %s:latest %s:%s" % (image_name, image_name, tag))
    run_command("systemctl restart %s" % container_name)

    # All images id under the image name
    image_id_all = get_container_image_id_all(image_name)

    # this is image_id for image with "latest" tag
    image_id_latest = get_container_image_id(image_latest)

    for id in image_id_all:
        if id != image_id_latest:
            # Unless requested, the previoud docker image will be preserved
            if not cleanup_image and id == image_id_previous:
                continue
            run_command("docker rmi -f %s" % id)

    exp_state = "reconciled"
    state = ""
    # post warm restart specific procssing for swss, bgp and teamd dockers, wait for reconciliation state.
    if warm_configured == True or warm:
        count = 0
        if warm_app_name:
            cmd = "docker exec -i database redis-cli -n 6 hget 'WARM_RESTART_TABLE|" + warm_app_name + "' state"
            # Wait up to 180 seconds for reconciled state
            while state != exp_state and count < 90:
                sys.stdout.write('\r')
                sys.stdout.write("[%-s" % ('='*count))
                sys.stdout.flush()
                count += 1
                time.sleep(2)
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
                state = proc.stdout.read().rstrip()
            sys.stdout.write("]\n\r")
            if state != exp_state:
                click.echo("%s failed to reach %s state"%(warm_app_name, exp_state))
    else:
        exp_state = ""  # this is cold upgrade

    # Restore to previous cold restart setting
    if warm_configured == False and warm:
        if container_name == "swss" or container_name == "bgp" or container_name == "teamd":
            run_command("config warm_restart disable %s" % container_name)

    # Currently restarting swss service doesn't bring back those dependent services, bring up them explicitly here.
    if container_name == "swss":
        services = [
            'radv',
            'snmp',
            'dhcp_relay',
        ]
        for service in services:
            try:
                run_command("systemctl start %s" % service)
            except SystemExit as e:
                click.echo('Failed')
                log_error("Start {} failed with error {}".format(service, e))
                raise

    if state == exp_state:
        click.echo('Done')
    else:
        click.echo('Failed')
        sys.exit(1)


# rollback docker image
@cli.command()
@click.option('-y', '--yes', is_flag=True, callback=abort_if_false,
        expose_value=False, prompt='Docker image will be rolled back, continue?')
@click.argument('container_name', metavar='<container_name>', required=True,
    type=click.Choice(["swss", "snmp", "lldp", "bgp", "pmon", "dhcp_relay", "telemetry", "teamd", "radv"]))
def rollback_docker(container_name):
    """ Rollback docker image to previous version"""
    image_name = get_container_image_name(container_name)
    # All images id under the image name
    image_id_all = get_container_image_id_all(image_name)
    if len(image_id_all) != 2:
        click.echo("Two images required, but there are '{}' images for '{}'. Aborting...".format(len(image_id_all), image_name))
        raise click.Abort()

    image_latest = image_name + ":latest"
    image_id_previous = get_container_image_id(image_latest)

    version_tag = ""
    for id in image_id_all:
        if id != image_id_previous:
            version_tag = get_docker_tag_name(id)

    # make previous image as latest
    run_command("docker tag %s:%s %s:latest" % (image_name, version_tag, image_name))
    if container_name == "swss" or container_name == "bgp" or container_name == "teamd":
        click.echo("Cold reboot is required to restore system state after '{}' rollback !!".format(container_name))
    else:
        run_command("systemctl restart %s" % container_name)

    click.echo('Done')


if __name__ == '__main__':
    cli()
