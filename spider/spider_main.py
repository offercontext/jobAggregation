# -*- coding: utf-8 -*-
import configparser
import csv
import json
import os
import random
import threading
import time
import traceback
from multiprocessing import Process, Queue
from threading import Thread

import bs4
import requests
from lxml import etree

from .tool import log, timer


class SpiderMeta(type):
    spiders = []

    def __new__(cls, name, bases, attrs):
        cls.spiders.append(type.__new__(cls, name, bases, attrs))
        return type.__new__(cls, name, bases, attrs)


class BaseSpider(object):
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;'
                  'q=0.9,image/webp,image/apng,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/64.0.3282.119 Safari/537.36',
        'Upgrade-Insecure-Requests': '1',
    }

    request_sleep = 0.7
    _time_recode = 0
    number = 0

    def request(self, method='get', url=None, encoding=None, **kwargs):

        if not kwargs.get('headers'):
            kwargs['headers'] = self.headers

        if not kwargs.get('timeout'):
            kwargs['timeout'] = 5

        rand_multi = random.uniform(0.8, 1.2)
        interval = time.time() - self._time_recode
        if interval < self.request_sleep:
            time.sleep((self.request_sleep - interval) * rand_multi)

        resp = getattr(requests, method)(url, **kwargs)
        self._time_recode = time.time()

        self.number = self.number + 1

        if encoding:
            resp.encoding = encoding
        return resp.text


class Job51Spider(BaseSpider, metaclass=SpiderMeta):
    request_sleep = 0

    def run(self):
        conf = configparser.ConfigParser()
        conf.read('./spider/conf.ini')
        citycode = conf['citycode'][self.city]
        page = 1
        # 获得总页数
        url = "https://search.51job.com/list/{},000000,0100%252C2400%252C2700%252C2500,00,9,99,{},2," \
              "{}.html?lang=c&stype=&postchannel=0000&workyear=99&cotype=99&degreefrom=99&jobterm=99&companysize=99" \
              "&providesalary=99&lonlat=0%2C0&radius=-1&ord_field=0&confirmdate=9&fromType=&dibiaoid=0&address=&line" \
              "=&specialarea=00&from=&welfare=".format(citycode, self.job, page)
        a = self.request(url=url, method='get', encoding='GBK')

        js = etree.HTML(a).xpath('/html/body/script[2]/text()')[0]  # 注意解析变成html里的js变量了
        jsonCode = js.partition('=')[2].strip()
        json_res = json.loads(jsonCode)

        maxpage = eval(json_res['total_page'])

        # 解析页数
        while True:
            url = "https://search.51job.com/list/{},000000,0100%252C2400%252C2700%252C2500,00,9,99,{},2," \
                  "{}.html?lang=c&stype=&postchannel=0000&workyear=99&cotype=99&degreefrom=99&jobterm=99&companysize=99" \
                  "&providesalary=99&lonlat=0%2C0&radius=-1&ord_field=0&confirmdate=9&fromType=&dibiaoid=0&address=&line" \
                  "=&specialarea=00&from=&welfare=".format(citycode, self.job, page)
            self.get_urls(url)

            log.printlog('多线程+' + str(page) + '页完成--' + self.city + self.job)
            page = page + 1
            if page == maxpage + 1:
                break
        return 'over'

    def get_urls(self, url):
        try:
            a = self.request(url=url, method='get', encoding='GBK')
            js = etree.HTML(a).xpath('/html/body/script[2]/text()')[0]  # 注意解析变成html里的js变量了
            jsonCode = js.partition('=')[2].strip()
            json_res = json.loads(jsonCode)
            urls = [i['job_href'] for i in json_res['engine_search_result']]
            if threading.activeCount() > 10:
                log.printlog(str(threading.activeCount()) + '线程存在，请注意检查程序外部阻塞原因')
                time.sleep(3)
            if self.threads:
                for i in urls:
                    t = threading.Thread(target=self.get_job_detail, args=(i,))
                    t.start()
                    time.sleep(0.03)
            else:
                for i in urls:
                    self.get_job_detail(i)
        except Exception as e:
            traceback.print_exc()
            time.sleep(2)
            self.get_urls(url)

    def get_job_detail(self, url):
        if 'jobs' not in url:
            return
        try:
            while True:
                try:
                    a = self.request(url=url, method='get', encoding='GBK')
                    html = etree.HTML(a)
                    break
                except:
                    time.sleep(3)
            try:
                pay = html.xpath('/ html / body / div[3] / div[2] / div[2] / div / div[1] / strong/text()')[0].strip()
            except:
                pay = ''
            list1 = html.xpath('/html/body/div[3]/div[2]/div[2]/div/div[1]/p[2]/@title')[0].split("|")

            list1 = [i.strip() for i in list1]
            if '招' in list1[2]:
                education = None
            else:
                education = list1[2]
            result = {
                'keyword': self.job,
                'provider': '前程无忧网',
                'place': self.city,
                'title': html.xpath('/html/body/div[3]/div[2]/div[2]/div/div[1]/h1/text()')[0].strip(),
                'salary': pay,
                'experience': list1[1],
                'education': education,
                'companytype': html.xpath('/html/body/div[3]/div[2]/div[4]/div[1]/div[2]/p[1]/text()')[0].strip(),
                'industry': html.xpath('/html/body/div[3]/div[2]/div[4]/div[1]/div[2]/p[3]/text()')[0].strip(),
                'description': html.xpath(' / html / body / div[3] / div[2] / div[3] / div[1] / div')[0].xpath(
                    'string(.)').strip().replace('"', '').strip().replace('\t', '').replace('\r', '').replace('\n', '')
            }
            self.queue.put(result)
            return
        except:
            time.sleep(3)
            return


class QiluSpider(BaseSpider, metaclass=SpiderMeta):
    request_sleep = 3

    def run(self):
        keys = [26, 2511, 24]
        for key in keys:
            page = 1

            while True:
                detail_list = self.get_page(page, key)
                if detail_list == []:
                    return 'over'
                page = page + 1
                # 错了
                while detail_list != []:
                    detail = [i.get_text(strip=True) for i in detail_list]
                    del detail_list[:9]
                    self.get_detail(detail)

    def get_page(self, page, key):
        pageurl = 'http://www.qlrc.com/personal/js/ajaxPager'
        pagedata = {
            'txtKeyWord': self.job,
            'oldRegionID': 32,
            'iddcIndustryID': '31 32 1 33 34',
            'idSFrom': 1310,
            'type': 0,
            'page': page
        }
        html = self.request('post', url=pageurl, data=pagedata)
        soup = bs4.BeautifulSoup(html, "html.parser")
        detail_list = soup.select('.JobList table td')
        return detail_list

    def get_detail(self, detail):

        list1 = detail[7].split('|')
        result = {
            'keyword': self.job,
            'provider': '齐鲁人才网',
            'place': detail[3],
            'title': detail[0],
            'salary': detail[4],
            'experience': list1[1].strip(),
            'education': list1[0].strip(),
            'description': list1[3].strip()
        }
        self.queue.put(result)


class BaiduSpider(BaseSpider, metaclass=SpiderMeta):
    request_sleep = 1

    def run(self):
        i = 0
        while True:
            url = 'http://zhaopin.baidu.com/api/qzasync?query={}&city={}&pcmod=1&pn={}&rn=50&sort_type=1'.format(
                self.job, self.city, i * 50)
            if i * 50 >= 760:
                return 'over'
            i = i + 1
            self.get_job_detail(url)

    def get_job_detail(self, url):
        html = self.request(url=url, method='get')
        try:
            dict1 = json.loads(html)
        except Exception as e:
            return

        dict2 = dict1['data']['disp_data']

        for i in dict2:
            if 'jobfirstclass' not in i.keys():
                i['jobfirstclass'] = ''
            result = {
                'provider': i['provider'],
                'keyword': self.job,
                'place': self.city,
                'title': i['title'],
                'salary': i['ori_salary'],
                'experience': i['ori_experience'],
                'education': i['ori_education'],
                'companytype': i['employertype'],
                'industry': i['jobfirstclass']
            }
            self.queue.put(result)


class SpiderProcess(Process):

    def __init__(self, data_queue, job, city, type, threads):
        Process.__init__(self)
        self.data_queue = data_queue
        self.job = job
        self.city = city
        self.type = type

        self.threads = threads

    def iter_spider(self, spider, spider_count):
        setattr(spider, 'job', self.job)
        setattr(spider, 'city', self.city)
        setattr(spider, 'threads', self.threads)
        setattr(spider, 'queue', self.data_queue)
        error = 0
        result = spider.run()
        if result == 'over':
            self.data_queue.put('over')
            error += 1
        if error == 10:
            log.printlog('%s-%s-%s- 爬虫已结束' % (spider.__class__.__name__, self.city, self.job))
            return

    def run(self):
        spiders = []

        if '51' in self.type:
            spiders.append(SpiderMeta.spiders[0]())
        if 'qilu' in self.type:
            spiders.append(SpiderMeta.spiders[1]())
        if 'baidu' in self.type:
            spiders.append(SpiderMeta.spiders[2]())

        spider_count = len(spiders)
        threads = []
        for i in range(spider_count):
            t = Thread(target=self.iter_spider, args=(spiders[i], spider_count,))
            t.setDaemon(True)
            t.start()
            threads.append(t)
        while True:
            if len([True for i in threads if i.is_alive() == False]) == spider_count:
                break

            time.sleep(2)

        # return


class WriterProcess(Process):
    """写数据进程"""

    def __init__(self, data_queue, number, type=None, spider_process=None, spider_count=None):
        Process.__init__(self)
        self.data_queue = data_queue
        self.type = type
        self.number = number
        self.spider_process = spider_process
        self.spider_count = spider_count

    def run(self):
        id, over = 1, 0
        with open('data/test.csv', 'a+', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            while True:
                if id == self.number + 1:
                    f.close()
                    return
                result = self.data_queue.get()
                if result == 'over':
                    over = over + 1
                    if over == self.spider_count:
                        f.close()
                        return
                elif result:
                    row = [
                        result.get('provider'), result.get('keyword'), result.get('title'), result.get('place'),
                        result.get('salary'), result.get('experience'), result.get('education'),
                        result.get('companytype'), result.get('industry'), result.get('description')
                    ]
                    id = id + 1
                    writer.writerow(row)


def main(dict_parameter):
    queue = Queue()

    jobs = ['软件', '图像', '自然语言处理', '人工智能', '学习', '前端', '后端', '数据', '算法', '测试', '网络安全', '运维', 'UI', '区块链', '网络', '全栈',
            '硬件', 'Java', 'C++', 'PHP', 'C#', '.NET', 'Hadoop', 'Python', 'Perl', 'Ruby', 'Nodejs', 'Go', 'Javascript',
            'Delphi', 'jsp', 'sql']

    citys = ['北京', '深圳', '广州', '杭州', '武汉', '成都', '南京', '苏州', '西安', '长沙', '重庆', '合肥', '东莞', '无锡', '福州', '大连', '宁波',
             '郑州', '济南', '天津', '佛山', '昆山', '沈阳', '青岛', '珠海', '厦门', '昆明', '南昌', '常州', '中山', '南宁', '惠州', '长春', '哈尔滨',
             '嘉兴', '石家庄', '贵阳', '南通', '张家港', '兰州', '海口', '江门', '温州', '徐州', '扬州', '太原', '烟台', '镇江', '泉州', '唐山', '绵阳',
             '太仓', '洛阳', '金华', '台州', '湖州', '柳州', '威海', '芜湖', '义乌', '保定', '泰州', '秦皇岛', '咸阳', '株洲', '韶关', '常熟', '澳门',
             '湘潭', '宜昌', '香港', '盐城', '潍坊', '襄阳', '绍兴', '马鞍山', '三亚', '汕头', '宿迁', '鹰潭', '乌鲁木齐', '连云港', '呼和浩特', '德阳', '岳阳',
             '靖江', '延安', '莆田', '新乡', '桂林', '盘锦', '鄂州', '滁州', '玉林', '黄石', '邢台', '云浮', '大理', '九江', '自贡', '济宁', '漳州', '揭阳',
             '银川', '梅州', '鄂尔多斯', '宜春', '上饶', '鞍山', '枣庄', '六安', '荆门', '赣州', '龙岩', '西宁', '孝感', '德州', '南平', '泰安', '菏泽',
             '阜阳', '拉萨', '清远', '宿州', '丽水', '铜陵', '湛江', '沧州', '黄山', '阿克苏', '舟山', '安庆', '临沂', '衢州', '南阳', '肇庆', '随州',
             '吉安', '兴安盟', '萍乡', '攀枝花', '承德', '上海']

    if os.path.exists('./data/test.csv'):
        try:
            os.remove('./data/test.csv')
            os.remove('./static/html/data.html')
        except:
            pass
    with open('data/test.csv', 'a+', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            ['provider', 'keyword', 'title', 'place', 'salary', 'experience', 'education', 'companytype',
             'industry', 'description'])
    total = eval(dict_parameter.get('total')[0])  #todo 不知道为什么服务器上不需要【0】
    number = eval(dict_parameter.get('number')[0])   #todo 不知道为什么服务器上不需要【0】
    if dict_parameter.get('threads'):
        threads = True
    else:
        threads = None
    no = 1
    for city in citys:
        for job in jobs:
            if dict_parameter.get('time'):
                timer.main(beginhour=eval(dict_parameter.get('hour')[0]),
                           beginminute=eval(dict_parameter.get('minute')[0]))
            spider_type = dict_parameter.get('type')
            p1 = SpiderProcess(queue, job, city, type=spider_type, threads=threads)
            p2 = WriterProcess(queue, number=number, spider_process=p1, spider_count=len(spider_type))
            p1.start()
            p2.start()
            p2.join()
            log.printlog(string=city + job + '爬取完成')
            p1.terminate()
            if no * number >= total:
                os.system('csvtotable ./data/test.csv ./static/html/data.html')
                p2.terminate()
                return
            p1.join()
            no = no + 1


if __name__ == '__main__':
    main()
