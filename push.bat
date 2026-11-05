@echo off
:: 解决中文乱码问题
chcp 65001 >nul

:: 添加所有修改
git add .

:: 提交代码（用英文防止乱码）
git commit -m "Auto update from local"

:: 强制关联并推送（一劳永逸）
git push -u origin main

echo.
echo ✅ 推送完成！
pause