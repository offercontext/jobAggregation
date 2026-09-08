from logging import *
from random import randint

import requests

global logger, hander, console
logger = getLogger(__name__)
hander = FileHandler('temp.txt')

logger.setLevel(INFO)
hander.setLevel(INFO)

formmatter = Formatter('%(asctime)s %(message)s')
hander.setFormatter(formmatter)
logger.addHandler(hander)


def push(string='', text=''):
    text = 'ID ' + str(randint(0, 999)) + '  ' + text
    url = 'https://pushbear.ftqq.com/sub'
    data = {'sendkey': '5106-f0e4971ad40f466108ec2cb182fb7640', 'text': string, 'desp': text}
    requests.post(url=url, data=data)


def printlog(string='', text='', filename=''):
    print(string + text)


def easypush(string, filename='', text='', ):
    print(string + text)
    push(string=string, text=text)


if __name__ == '__main__':
    easypush(string='我爱你啊', filename='log', text='emmm')
