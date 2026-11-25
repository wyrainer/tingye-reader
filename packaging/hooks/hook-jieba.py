from PyInstaller.utils.hooks import collect_data_files

# Standard Chinese word segmentation uses the dictionary and Python modules.
datas = collect_data_files("jieba", includes=["dict.txt"])
excludedimports = ["jieba.lac_small", "jieba.analyse", "jieba.posseg", "paddle"]
