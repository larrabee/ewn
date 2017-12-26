# -*- coding: utf-8 -*-
import subprocess
import sys, os
import signal
import socket
import datetime, time
import smtplib
from email.mime.text import MIMEText
import argparse
from tempfile import gettempdir
import hashlib
import json
from gelfclient import UdpClient

def main():
    cli_parser = argparse.ArgumentParser()
    cli_parser.add_argument('-r', '--recipients', nargs='+', type=str, default=None, help='list of emails')
    cli_parser.add_argument('--comment', type=str, default=None, help='Comment for email message')
    cli_parser.add_argument('-c', '--command', type=str, required=True, help='Shell command')
    cli_parser.add_argument('--valid-exitcodes', type=int, nargs='+', default=[0, ], help='Valid exitcodes for executed command')
    cli_parser.add_argument('-d', '--daemon', default=False, action='store_true', help='Daemonize process after start')
    cli_parser.add_argument('--dont-duplicate', default=False, action='store_true', help='Not run process when process with same name already run')
    cli_parser.add_argument('--retry', type=int, default=1, help='Retry run N times on fail')
    cli_parser.add_argument('--retry-sleep', type=int, default=0, help='Sleep between retries (seconds)')
    cli_parser.add_argument('--config', type=str, default='/etc/ewn.conf', help='Config file')
    cli = cli_parser.parse_args()

    config = json.loads(open(cli.config, 'r').read())
    if cli.recipients is None:
        cli.recipients = config['email']['recipients']

    def popen(command):
        process = subprocess.Popen(str(command), stderr=subprocess.STDOUT, stdout=subprocess.PIPE, shell=True, executable='/bin/bash')
        output = process.communicate()[0]
        return process.returncode, output

    def sighup_handler(signal, frame):
        pass

    class RunFile():
        def __init__(self):
            self.file_name = hashlib.sha1(cli.command).hexdigest()
            self.tmp_file_path = '{0}/ewn-{1}.lock'.format(gettempdir(), self.file_name)
        
        def create(self):
            tfile = open(self.tmp_file_path, 'w')
            tfile.write(str(os.getpid()))
            tfile.close()

        def remove(self):
            try:
                os.remove(self.tmp_file_path)
            except OSError:
                pass

        def lock_exist(self):
            if (os.path.isfile(self.tmp_file_path)):
                pid = open(self.tmp_file_path, 'r').read()
                if (self.__pid_exist(pid)):
                    return True
            return False
        
        @staticmethod
        def __pid_exist(pid):
            if os.path.isdir('/proc/{}'.format(pid)):
                return True
            return False

    def send_to_email(subject, message):
        msg = MIMEText(message)
        msg['Subject'] = subject
        if ('from' in config['email']) and (config['email']['from'] is not None):
            msg['From'] = config['email']['from']
        else:
            msg['From'] = 'ewn@' + socket.gethostname()
        msg['To'] = ", ".join(cli.recipients)
        s = smtplib.SMTP(config['email']['host'])
        if config['email']['secure'] is True:
            s.ehlo()
            s.starttls(tuple())
            s.ehlo()
        s.login(str(config['email']['user']), str(config['email']['pass']))
        s.sendmail(msg['From'], cli.recipients, msg.as_string())
        s.quit()


    def send_to_zabbix(status):
        command = 'zabbix_sender -c {0} -k "{1}" -o "{2}"'.format(config['zabbix']['config'], config['zabbix']['key'], status)
        exitcode, output = popen(command)
        if exitcode != 0:
            raise RuntimeError('Zabbix sender failed with error: {0}'.format(output))
        return

    if cli.daemon:
        pid=os.fork()
        if pid != 0:
            print('Process successfully daemonized. Pid: {0}'.format(pid))
            exit(0)
        else:
            sys.stdout.close()
            sys.stderr.close()
            sys.stdin.close()
            signal.signal(signal.SIGHUP, sighup_handler)

    message = 'Command: {0}\nHost: {1}\nComment: {2}\nValid exit codes: {3}\nRetries: {4}\nRetry sleep: {5}\nDaemonized: {6}\nPreserve duplicates: {7}'.format(
        cli.command, socket.gethostname(), cli.comment, cli.valid_exitcodes, cli.retry, cli.retry_sleep, cli.daemon, cli.dont_duplicate)
    exitcode = None

    if (cli.dont_duplicate is False) or (RunFile().lock_exist() is False):
        RunFile().create()
        for retry in range(1, cli.retry+1):
            if exitcode not in cli.valid_exitcodes:
                start_date = datetime.datetime.now()
                exitcode, output = popen(cli.command)
                finish_date = datetime.datetime.now()
                message += '\n\nRetry number: {0}\nStart date: {1}\nFinish date: {2}\nDuration: {3}\nExit code: {4}\nOutput:\n{5}'.format(
                    retry, str(start_date), str(finish_date), str(finish_date - start_date), exitcode, output)
                try:
                    graylog = UdpClient(config['graylog']['host'], port=config['graylog']['port'], mtu=config['graylog']['mtu'])
                    data = {}
                    data['short_message'] = cli.command
                    data['command'] = cli.command
                    data['start_date'] = str(start_date)
                    data['finish_date'] = str(finish_date)
                    data['duration'] = (finish_date - start_date).seconds
                    data['exitcode'] = exitcode
                    if len(output) > 32768:
                        data['output'] = output[0:32700] + '...(Elasticsearch limit exceeded)'
                    else:
                        data['output'] = output
                    data['comment'] = cli.comment
                    data['retry'] = "{0}/{1}".format(retry, cli.retry)
                    data['tag'] = config['graylog']['tag']
                    if exitcode not in cli.valid_exitcodes: data['failed'] = 1
                    else: data['failed'] = 0
                    if config['graylog']['enabled']:
                        graylog.log(data)
                except:
                    print('Failed to send info to Graylog.')
                if exitcode not in cli.valid_exitcodes:
                    time.sleep(cli.retry_sleep)
        RunFile().remove()
    else:
        print('Duplicate process found, skipping start')
        exitcode = 300
        output = 'Duplicate process found, skipping start'
        message += '\nOutput: {0}'.format(output)
        try:
            graylog = UdpClient(config['graylog']['host'], port=config['graylog']['port'], mtu=config['graylog']['mtu'])
            data = {}
            data['short_message'] = cli.command
            data['command'] = cli.command
            data['start_date'] = str(start_date)
            data['finish_date'] = str(finish_date)
            data['duration'] = (finish_date - start_date).seconds
            data['exitcode'] = exitcode
            if len(output) > 32768:
                data['output'] = output[0:32700] + '...(Elasticsearch limit exceeded)'
            else:
                data['output'] = output
            data['comment'] = cli.comment
            data['retry'] = "{0}/{1}".format(retry, cli.retry)
            data['tag'] = config['graylog']['tag']
            if exitcode not in cli.valid_exitcodes: data['failed'] = 1
            else: data['failed'] = 0
            if config['graylog']['enabled']:
                graylog.log(data)
        except:
            print('Failed to send info to Graylog.')
        
    if cli.daemon is False:
        print(message)
    if exitcode not in cli.valid_exitcodes: 
        if config['email']['enabled']:
            try: send_to_email(subject='ewn@{0} FAILED: {1} '.format(socket.gethostname(), cli.command), message=message)
            except: print("Failed to send email.")
        if config['zabbix']['enabled']:
            try: send_to_zabbix(1)
            except: print("Failed to send info to Zabbix.")
    else:
        if config['zabbix']['enabled']:
            try: send_to_zabbix(0)
            except: print("Failed to send info to Zabbix.")
