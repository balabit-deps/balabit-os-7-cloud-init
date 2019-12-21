# Copyright (C) 2009-2010 Canonical Ltd.
# Copyright (C) 2012, 2013 Hewlett-Packard Development Company, L.P.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Haefliger <juerg.haefliger@hp.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

"""
Set Passwords
-------------
**Summary:** Set user passwords and enable/disable SSH password authentication

This module consumes three top-level config keys: ``ssh_pwauth``, ``chpasswd``
and ``password``.

The ``ssh_pwauth`` config key determines whether or not sshd will be configured
to accept password authentication.  True values will enable password auth,
false values will disable password auth, and the literal string ``unchanged``
will leave it unchanged.  Setting no value will also leave the current setting
on-disk unchanged.

The ``chpasswd`` config key accepts a dictionary containing either or both of
``expire`` and ``list``.

If the ``list`` key is provided, it should contain a list of
``username:password`` pairs.  This can be either a YAML list (of strings), or a
multi-line string with one pair per line.  Each user will have the
corresponding password set.  A password can be randomly generated by specifying
``RANDOM`` or ``R`` as a user's password.  A hashed password, created by a tool
like ``mkpasswd``, can be specified; a regex
(``r'\\$(1|2a|2y|5|6)(\\$.+){2}'``) is used to determine if a password value
should be treated as a hash.

.. note::
    The users specified must already exist on the system.  Users will have been
    created by the ``cc_users_groups`` module at this point.

By default, all users on the system will have their passwords expired (meaning
that they will have to be reset the next time the user logs in).  To disable
this behaviour, set ``expire`` under ``chpasswd`` to a false value.

If a ``list`` of user/password pairs is not specified under ``chpasswd``, then
the value of the ``password`` config key will be used to set the default user's
password.

**Internal name:** ``cc_set_passwords``

**Module frequency:** per instance

**Supported distros:** all

**Config keys**::

    ssh_pwauth: <yes/no/unchanged>

    password: password1
    chpasswd:
        expire: <true/false>

    chpasswd:
        list: |
            user1:password1
            user2:RANDOM
            user3:password3
            user4:R

    ##
    # or as yaml list
    ##
    chpasswd:
        list:
            - user1:password1
            - user2:RANDOM
            - user3:password3
            - user4:R
            - user4:$6$rL..$ej...
"""

import re
import sys

from cloudinit.distros import ug_util
from cloudinit import log as logging
from cloudinit.ssh_util import update_ssh_config
from cloudinit import util

from string import ascii_letters, digits

LOG = logging.getLogger(__name__)

# We are removing certain 'painful' letters/numbers
PW_SET = (''.join([x for x in ascii_letters + digits
                   if x not in 'loLOI01']))


def handle_ssh_pwauth(pw_auth, service_cmd=None, service_name="ssh"):
    """Apply sshd PasswordAuthentication changes.

    @param pw_auth: config setting from 'pw_auth'.
                    Best given as True, False, or "unchanged".
    @param service_cmd: The service command list (['service'])
    @param service_name: The name of the sshd service for the system.

    @return: None"""
    cfg_name = "PasswordAuthentication"
    if service_cmd is None:
        service_cmd = ["service"]

    if util.is_true(pw_auth):
        cfg_val = 'yes'
    elif util.is_false(pw_auth):
        cfg_val = 'no'
    else:
        bmsg = "Leaving ssh config '%s' unchanged." % cfg_name
        if pw_auth is None or pw_auth.lower() == 'unchanged':
            LOG.debug("%s ssh_pwauth=%s", bmsg, pw_auth)
        else:
            LOG.warning("%s Unrecognized value: ssh_pwauth=%s", bmsg, pw_auth)
        return

    updated = update_ssh_config({cfg_name: cfg_val})
    if not updated:
        LOG.debug("No need to restart ssh service, %s not updated.", cfg_name)
        return

    if 'systemctl' in service_cmd:
        cmd = list(service_cmd) + ["restart", service_name]
    else:
        cmd = list(service_cmd) + [service_name, "restart"]
    util.subp(cmd)
    LOG.debug("Restarted the ssh daemon.")


def handle(_name, cfg, cloud, log, args):
    if len(args) != 0:
        # if run from command line, and give args, wipe the chpasswd['list']
        password = args[0]
        if 'chpasswd' in cfg and 'list' in cfg['chpasswd']:
            del cfg['chpasswd']['list']
    else:
        password = util.get_cfg_option_str(cfg, "password", None)

    expire = True
    plist = None

    if 'chpasswd' in cfg:
        chfg = cfg['chpasswd']
        if 'list' in chfg and chfg['list']:
            if isinstance(chfg['list'], list):
                log.debug("Handling input for chpasswd as list.")
                plist = util.get_cfg_option_list(chfg, 'list', plist)
            else:
                log.debug("Handling input for chpasswd as multiline string.")
                plist = util.get_cfg_option_str(chfg, 'list', plist)
                if plist:
                    plist = plist.splitlines()

        expire = util.get_cfg_option_bool(chfg, 'expire', expire)

    if not plist and password:
        (users, _groups) = ug_util.normalize_users_groups(cfg, cloud.distro)
        (user, _user_config) = ug_util.extract_default(users)
        if user:
            plist = ["%s:%s" % (user, password)]
        else:
            log.warning("No default or defined user to change password for.")

    errors = []
    if plist:
        plist_in = []
        hashed_plist_in = []
        hashed_users = []
        randlist = []
        users = []
        # N.B. This regex is included in the documentation (i.e. the module
        # docstring), so any changes to it should be reflected there.
        prog = re.compile(r'\$(1|2a|2y|5|6)(\$.+){2}')
        for line in plist:
            u, p = line.split(':', 1)
            if prog.match(p) is not None and ":" not in p:
                hashed_plist_in.append(line)
                hashed_users.append(u)
            else:
                # in this else branch, we potentially change the password
                # hence, a deviation from .append(line)
                if p == "R" or p == "RANDOM":
                    p = rand_user_password()
                    randlist.append("%s:%s" % (u, p))
                plist_in.append("%s:%s" % (u, p))
                users.append(u)
        ch_in = '\n'.join(plist_in) + '\n'
        if users:
            try:
                log.debug("Changing password for %s:", users)
                chpasswd(cloud.distro, ch_in)
            except Exception as e:
                errors.append(e)
                util.logexc(
                    log, "Failed to set passwords with chpasswd for %s", users)

        hashed_ch_in = '\n'.join(hashed_plist_in) + '\n'
        if hashed_users:
            try:
                log.debug("Setting hashed password for %s:", hashed_users)
                chpasswd(cloud.distro, hashed_ch_in, hashed=True)
            except Exception as e:
                errors.append(e)
                util.logexc(
                    log, "Failed to set hashed passwords with chpasswd for %s",
                    hashed_users)

        if len(randlist):
            blurb = ("Set the following 'random' passwords\n",
                     '\n'.join(randlist))
            sys.stderr.write("%s\n%s\n" % blurb)

        if expire:
            expired_users = []
            for u in users:
                try:
                    cloud.distro.expire_passwd(u)
                    expired_users.append(u)
                except Exception as e:
                    errors.append(e)
                    util.logexc(log, "Failed to set 'expire' for %s", u)
            if expired_users:
                log.debug("Expired passwords for: %s users", expired_users)

    handle_ssh_pwauth(
        cfg.get('ssh_pwauth'), service_cmd=cloud.distro.init_cmd,
        service_name=cloud.distro.get_option('ssh_svcname', 'ssh'))

    if len(errors):
        log.debug("%s errors occured, re-raising the last one", len(errors))
        raise errors[-1]


def rand_user_password(pwlen=9):
    return util.rand_str(pwlen, select_from=PW_SET)


def chpasswd(distro, plist_in, hashed=False):
    if util.is_FreeBSD():
        for pentry in plist_in.splitlines():
            u, p = pentry.split(":")
            distro.set_passwd(u, p, hashed=hashed)
    else:
        cmd = ['chpasswd'] + (['-e'] if hashed else [])
        util.subp(cmd, plist_in)

# vi: ts=4 expandtab
