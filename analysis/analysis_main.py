import configparser
import os
import pymysql
import sys


class Analyze():
    process_fn_list=[]
    analyze_fn_list=[]
    chart_fn_list=[]

    conf = configparser.ConfigParser()
    
    # 以下配置请修改
    user = "root"
    password = "zym233521"
    
    
    db = pymysql.connect(host="localhost", user=user, password=password, charset="utf8")
    cursor = db.cursor()
    # cursor.execute("CREATE DATABASE `ujn_a` CHARACTER SET 'utf8';")
    cursor.execute('USE `ujn_a`;')

    path = os.getcwd().replace('\\', '/')

    @classmethod
    def main(cls):
        script_path = os.path.realpath(__file__)
        script_dir = os.path.dirname(script_path)
        sys.path.append(script_dir)
        input_data.main()

        process_data.main()

        analyze_data.main()

        test_analyze_data.main()


if __name__ == '__main__':
    pass
