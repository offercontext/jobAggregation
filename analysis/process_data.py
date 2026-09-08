import re
import jieba
import analysis_main as A


# 函数注册器
def ways(func):
    A.Analyze.process_fn_list.append(func)

    def wrapper(*args, **kw):
        return func(*args, **kw)

    return wrapper


def main():
    global cursor, db
    cursor = A.Analyze.cursor
    db = A.Analyze.db

    print(A.Analyze.process_fn_list)

    for fn in A.Analyze.process_fn_list:
        fn()
        print(fn.__name__ + '  ok')


@ways
def big_del_null():
    cursor.execute("update big set salary = '' where salary is null")
    db.commit()
    cursor.execute("update big set experience = '' where experience is null")
    db.commit()
    cursor.execute("update big set education = '' where education is null")
    db.commit()
    #    !!!!!注意 特殊的值有 None null
    cursor.execute("update big set experience = '不限' where experience = 'None'")
    db.commit()
    cursor.execute("update big set experience = '1' where experience = '-1'")
    db.commit()
    cursor.execute("update big set experience = '不限' where experience = 'null'")
    db.commit()
    cursor.execute("update big set education = '不限' where education = ''")
    db.commit()
    cursor.execute("update big set welfare = '' where welfare is null")
    db.commit()

@ways
def big_chuligongzi():
    sql = "select salary from big "  # 获取所有的工资信息
    cursor.execute(sql)
    data = cursor.fetchall()
    id = 1
    for i in data:
        pay = i[0]
        if pay.find('以上') != -1:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, pay)
            min = res[0]
            max = "不限"
            ave = res[0]
            sql = "UPDATE big SET MIN_PAY=" + str(min) + ",MAX_PAY='不限',AVE_PAY=" + str(ave) + " WHERE id=" + str(
                id)
            cursor.execute(sql)
            id += 1
            continue

        elif pay.find('-') != -1:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, pay)
            min = int(res[0])
            max = int(res[1])
            ave = int((min + max) // 2)

        elif pay.find('面议') != -1:
            min = '面议'
            max = '面议'
            ave = '面议'
            sql = "UPDATE big SET MIN_PAY='面议',MAX_PAY='面议',AVE_PAY='面议' WHERE id=" + str(id)
            cursor.execute(sql)
            id += 1
            continue
        else:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, pay)
            if res == None:
                min = '面议'
                max = '面议'
                ave = '面议'
                sql = "UPDATE big SET MIN_PAY='面议',MAX_PAY='面议',AVE_PAY='面议' WHERE id=" + str(id)
                cursor.execute(sql)
                id += 1
                continue
            else:
                min = res[0]
                max = res[0]
                ave = res[0]
        sql = "UPDATE big SET MIN_PAY=" + str(min) + ",MAX_PAY=" + str(max) + ",AVE_PAY=" + str(
            ave) + " WHERE id=" + str(id)
        cursor.execute(sql)
        if id % 5000 == 0:
            db.commit()
        id += 1
    db.commit()


@ways
def big_chulijingyan():  # 将经验提取出来
    sql = "SELECT experience FROM big"
    cursor.execute(sql)
    results = cursor.fetchall()
    a = 1
    for row in results:
        if row[0] == '':
            a += 1
            continue
        if row[0].isalpha() or row[0] == '无工作经验':
            sql = "update big set experience2 = '0' where id = '%s'" % str(a)
            cursor.execute(sql)
        elif row[0].find('-') != -1 and row[0].split('-')[0] != '' and row[0].split('-')[1] != '':
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, row[0])
            # try:
            res = int((int(res[0]) + int(res[1])) / 2)
            sql = "update big set experience2 = " + str(res) + " where id = '%s'" % str(a)
            cursor.execute(sql)
        else:
            print(row[0], type(row[0]))
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, row[0])
            sql = "update big set experience2 = " + str(res[0]) + " where id = '%s'" % str(a)
            cursor.execute(sql)
            # db.commit()
        print(a)
        if int(a) % 5000 == 0:
            db.commit()
        a += 1
    db.commit()


@ways
def qlrc():
    cursor.execute("select place from qlrc")
    re = cursor.fetchall()
    id = 1
    for i in range(0, len(re)):
        if len(re[i][0]) == 0 or re[i][0].find('市') == -1:
            id += 1
            continue
        else:
            x = list(jieba.cut(re[i][0]))
            x = x[1]
            cursor.execute("update qlrc set city = '" + str(x) + "' where id =" + str(id))
            db.commit()
            id += 1

    cursor.execute("select pay from qlrc")  # 获取所有的工资信息
    data = cursor.fetchall()
    id = 1
    for i in data:
        pay = i[0]
        if pay.find('-') != -1:
            pay_min = float(((pay.split('-')[0]).split('K'))[0]) * 1000
            pay_max = float((pay.split('-')[1].split('K'))[0]) * 1000
            ave = round((pay_max + pay_min) / 2, 2)
            sql = "UPDATE qlrc SET MIN_PAY=" + str(pay_min) + ",MAX_PAY=" + str(pay_max) + ",AVE_PAY=" + str(
                ave) + " WHERE id=" + str(id)
            cursor.execute(sql)
            db.commit()
        id += 1





@ways
def qcwy1():
    cursor.execute("select xexperience,number from qcwy")
    data = cursor.fetchall()
    id = 1
    for i in data:
        # 处理人数
        if i[1].isalpha():
            res = 0
            sql = "update qcwy set number = " + str(res) + " where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        elif i[1].find('-') != -1:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, i[1])
            res = int((int(res[0]) + int(res[1])) / 2)
            sql = "update qcwy set number = " + str(res) + " where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        else:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, i[1])
            sql = "update qcwy set number = " + str(res[0]) + " where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        # 提经验值
        if i[0].isalpha():
            sql = "update qcwy set experience = '0' where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        elif i[0].find('-') != -1:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, i[0])
            res = int((int(res[0]) + int(res[1])) / 2)
            sql = "update qcwy set experience = " + str(res) + " where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        else:
            pattern = re.compile(r'\d+')
            res = re.findall(pattern, i[0])
            sql = "update qcwy set experience = " + str(res[0]) + " where id = %s" % str(id)
            cursor.execute(sql)
            # db.commit()
        if int(id) % 100000 == 0:
            db.commit()
        print(id)
        id += 1
    db.commit()

    cursor.execute("SELECT number FROM qcwy ")
    results = cursor.fetchall()
    sum = 0
    count = 0
    for row in results:
        if int(row[0]) != 0:
            sum += int(row[0])
            count += 1
    b = int(sum / count)
    print(b)
    id = 1
    for row in results:
        if int(row[0]) != 0:
            id += 1
            continue
        else:
            sql = "update qcwy set number = " + str(b) + " where id = %s" % str(id)
            cursor.execute(sql)
        if int(id) % 100000 == 0:
            db.commit()
        print(id)
        id += 1
    db.commit()


@ways
def qcwy2():
    cursor.execute('select salary from qcwy ')
    data = cursor.fetchall()
    id = 1
    for i in data:
        pay = i[0]
        if pay.find('千') != -1 or pay.find('万') != -1 or pay.find('元') != -1:  # 如果为空值不处理，当字符串中存在千、万的时候就进行如下处理
            if pay.find('年') != -1:
                x = 12
            elif pay.find('天') != -1:
                x = 1 / 30
            else:
                x = 1
            if pay.find('千') != -1:
                if pay.find('-') != -1:
                    pay_min = pay.split('-')[0]
                    pay_max = pay.split('-')[1].split('千')[0]
                    min = float(pay_min) * 1000 / x
                    max = float(pay_max) * 1000 / x
                    ave = (min + max) / 2
                else:
                    min = max = ave = float(pay.split('千')[0]) * 1000 / x
            elif pay.find('万') != -1:
                if pay.find('-') != -1:

                    pay_min = pay.split('-')[0]
                    pay_max = pay.split('-')[1].split('万')[0]
                    min = float(pay_min) * 10000 / x
                    max = float(pay_max) * 10000 / x
                    ave = (min + max) / 2
                else:
                    min = max = ave = float(pay.split('万')[0]) * 10000 / x
            else:
                if pay.find('-') != -1:
                    pay_min = pay.split('-')[0]
                    pay_max = pay.split('-')[1].split('元')[0]
                    min = float(pay_min) / x
                    max = float(pay_max) / x
                    ave = (min + max) / 2
                else:
                    min = max = ave = float(pay.split('元')[0]) / x

            # sql语句是将min max ave放入数据库
            sql = "UPDATE qcwy SET MIN_PAY=" + str(min) + ",MAX_PAY=" + str(max) + ",AVE_PAY=" + str(
                ave) + " WHERE id=" + str(id)
            cursor.execute(sql)

        if int(id) % 100000 == 0:
            db.commit()
        print(id)
        id += 1
    db.commit()


@ways
def qcwy_view():
    def duquxieru(x, y):
        count = 0
        for i in x:
            info = []
            i = str(i)
            m = y[count]
            j = '`' + m + '`'
            if i == '数据':
                sql = 'create view ' + j + ' as select distinct * from qcwy where (qcwy.title like "%' + i + '%" and qcwy.title not like "%数据管理%")'
                print(sql)
                cursor.execute(sql)
            elif i == '维修':
                sql = 'create view ' + j + ' as select distinct * from qcwy where (qcwy.title like "%' + i + '%" or qcwy.title like "%维护%")'
                print(sql)
                cursor.execute(sql)
            else:

                # sql = 'create view ' + j + ' as select distinct * from qcwy where (qcwy.title like "%' + i + '%" or qcwy.title like "%' + i2 + '%"' \
                # ' or qcwy.title like "%' + i3 + '%" or or qcwy.title like "%' + i4 + '%")'

                sql2 = 'create view ' + j + ' as select distinct * from qcwy where (qcwy.title like "%' + i + '%")'
                print(sql2)

                cursor.execute(sql2)

            count += 1

    sql = "SELECT * FROM qcwy"
    cursor.execute(sql)
    results = cursor.fetchall()
    results = list(results)

    l1 = ['讲师', '经理', '总监', '大数据', '负责人', '服务器', '数据库', '软件', '建模', '硬件', '网络', '人工智能', '后端', '机器学习']
    l1_1 = ['XXXX讲师', '项目开发经理', '技术/研发总监', '大数据开发工程师', '技术/研究/项目负责人', '服务器工程师', '数据库工程师', '软件开发工程师', '建模工程师', '硬件工程师',
            '网络工程师', '人工智能开发工程师', '后端工程师', '机器学习工程师']
    l2 = ['数据', '数据管理', '前端', '维修']
    l2_1 = ['数据挖掘/分析/处理工程师', '数据管理工程师', 'Web前端工程师', '计算机维修/维护工程师']
    l3 = ['Java', 'C++', 'PHP', 'C#', '.Net', 'Hadoop', 'Python', 'Perl', 'Ruby', 'Nodejs', 'Go', 'Javascript',
          'Delphi', 'jsp', 'sql', 'Linux', 'Android', 'IOS', 'GIS', 'BI']
    l3_1 = ['Java工程师', 'C++工程师', 'PHP工程师', 'C#工程师', '.NET工程师', 'Hadoop工程师', 'Python工程师', 'Perl工程师', 'Ruby工程师',
            'Nodejs工程师', 'Go工程师', 'Javascript工程师', 'Delphi工程师', 'jsp工程师', 'sql工程师', 'Linux开发工程师', 'Android开发工程师',
            'IOS开发工程师', 'GIS开发/研发工程师', 'BI工程师']

    results = duquxieru(l1, l1_1)
    print(1)

    results = duquxieru(l2, l2_1)
    print(2)

    results = duquxieru(l3, l3_1)
    print(3)


@ways
def oldnew_view():
    l3 = ['传统', '新兴']
    for i in l3:
        if i == '新兴':  # todo 改数量
            sql = 'create view `新兴职业` as select distinct ID ,title,ave_pay,number,experience,education ' \
                  'from qcwy where (qcwy.title like "%学习%"  or qcwy.title like "%人工智能%" ' \
                  'or qcwy.title like "%数据%" or qcwy.title like "%算法%" or qcwy.title like "%区块链%"' \
                  ' or qcwy.title like "%视觉%" or qcwy.title like "%物联网%" or qcwy.title like "%自然语言%"' \
                  'and qcwy.id < "10000")'
        else:
            sql = 'create view `传统职业` as select distinct ID ,title,ave_pay,number,experience,education from qcwy where (qcwy.title not like "%学习%"  and qcwy.title not like "%人工智能%" ' \
                  'and qcwy.title  not like "%数据%" and qcwy.title not like "%算法%" and qcwy.title not like "%区块链%"' \
                  '  and qcwy.title not like "%视觉%" and qcwy.title not like "%物联网%" and qcwy.title not like "%自然语言%"' \
                  'and qcwy.id < "10000")'
        cursor.execute(sql)

@ways
def bigcompany_bigdata_view():
    l1 = ['华为', '搜狐', '滴滴出行', '金山', '新浪', '美团', '甲骨文', '阿里巴巴', '微软', '爱奇艺', '中国电信', '百度', '奇虎', '英特尔', '完美世界', '字节跳动',
          '中国移动', '中国联通', '巨人网络', '浪潮', '联想', '搜狗', '蚂蚁金服', '腾讯', '网易', '小米', 'IBM', '京东']
    for i in l1:
        j = '`' + i + '`'
        cursor.execute('DROP VIEW IF EXISTS ' + j)
        sql = 'create view ' + j + ' as select distinct * from big where (big.company like "%' + i + '%")'
        cursor.execute(sql)
    sql = 'create view `大数据职位` as select distinct ID ,place,number,industry from qcwy where (qcwy.title like "%数据%" )'
    cursor.execute(sql)

@ways
def another_view():
    l1 = ['软件', '人工智能', '学习', '前端', '后端', '数据', '算法', '测试', '安全', '运维', 'UI', '区块链', '网络', '全栈', '硬件', '物联网', '游戏']
    for i in l1:
        j = '`' + i + '`'
        if i == '学习':  # 机器学习和深度学习都是学习
            #                  视图名                                         原表名         原表名
            sql = 'create view `深度\机器学习` as select distinct * from qcwy where (qcwy.title like "%学习%")'
        elif i == 'UI':
            sql = 'create view `UI` as select distinct * from qcwy where (qcwy.title like "%UI%" and qcwy.title not like "%GUI%")'
        elif i == '软件':
            sql = 'create view `软件开发` as select distinct * from qcwy where (qcwy.title like "%软件%" and qcwy.title not like "%测试%")'
        else:
            sql = 'create view ' + j + ' as select distinct * from qcwy where (qcwy.title like "%' + i + '%")'
        cursor.execute(sql)
