import analysis_main

# 部分代码注释掉了 正式时取消注释

def main():
    for name in ['qcwy', 'qlrc', 'big']:
        analysis_main.Analyze.cursor.execute('DROP TABLE IF EXISTS {}; '.format(name))
        if name == 'qcwy':
            sql_1 = '''
            CREATE TABLE `qcwy`  (
              `id` int(11) NOT NULL AUTO_INCREMENT,
              `title` varchar(255),
              `place` varchar(255),
              `salary` varchar(255),
              `xexperience` varchar(255),
              `experience` varchar(255),
              `education` varchar(255),
              `number` varchar(255) ,
              `companytype` varchar(255),
              `industry` varchar(255),
              `description` text,
              `MAX_PAY` double(10, 0) ,
              `MIN_PAY` double(10, 0) ,
              `AVE_PAY` double(10, 0) ,
              `work` varchar(255) ,
              `flag` int(6) DEFAULT 0,
              PRIMARY KEY (`id`)
            );'''
            columns = '(title,place,salary,xexperience,education,number,companytype,industry,description)'
        if name == 'big':
            sql_1 = '''
            CREATE TABLE `big`  (
              `id` int(11) NOT NULL AUTO_INCREMENT,
              `job` varchar(255) ,
              `company` varchar(255) ,
              `salary` varchar(255),
              `experience` varchar(255) ,
              `education` varchar(255) ,
              `welfare` varchar(255) ,
              `experience2` varchar(255),
              `min_pay` varchar(100),
              `max_pay` varchar(100) ,
              `ave_pay` varchar(100),
              PRIMARY KEY (`id`) 
            );'''
            columns = '(job,company,salary,experience,education,welfare)'
        if name == 'qlrc':
            sql_1 = '''
            CREATE TABLE `qlrc`  (
              `id` int(11) NOT NULL AUTO_INCREMENT,
              `job` varchar(255) ,
              `company` varchar(255) ,
              `place` varchar(255) ,
              `pay` varchar(255) ,
              `lessinfo` varchar(255) ,
              `city` varchar(255) ,
              `min_pay` double(12, 0) ,
              `ave_pay` double(12, 0) ,
              `max_pay` double(12, 0),
              PRIMARY KEY (`id`)
            ); '''
            columns = '(job,company,place,pay,lessinfo)'

        analysis_main.Analyze.cursor.execute(sql_1)

        print(analysis_main.Analyze.path)

        sql_2 = '''LOAD DATA INFILE  "{0}/data/{1}.csv" into table `{2}` 
        fields terminated by "," optionally enclosed by '"' escaped by '"' lines terminated by '\r\n' 
         {3} ;'''.format('C:/Users/Administrator/Desktop/process/job/', name, name, columns)
        analysis_main.Analyze.cursor.execute(sql_2)
        analysis_main.Analyze.db.commit()


if __name__ == '__main__':
    main()