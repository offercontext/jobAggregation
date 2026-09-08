import datetime
import time


# beginhour beginminute开始时间  overhour overminute结束时间  wait sleep检测时间频率
def main(beginhour=0, overhour=0, beginminute=0, overminute=0, wait=5):
    if overhour == 0 and overminute == 0:
        while True:
            now = datetime.datetime.now()
            if now.hour == beginhour and now.minute == beginminute:  # 到达设定时间，进入函数外循环
                break
            time.sleep(wait)  # 等几秒后检测
        return
    else:
        now = datetime.datetime.now()
        if now.hour == overhour and now.minute == overminute:
            while True:
                now = datetime.datetime.now()
                if now.hour == beginhour and now.minute == beginminute:
                    break
                time.sleep(wait)
        else:
            return


if __name__ == '__main__':
    for i in range(500):
        time.sleep(2)
        print(i)
        main(beginhour=22, beginminute=34, overhour=22, overminute=35)
