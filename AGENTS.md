1. 远程 VPS 机器 (无密码登录 )
  ssh myvps2 
   PROJECT_ROOT in myvps2: /voxstudio/attendee
2. 使用uv管理项目和依赖
3. 所有的代码修改要现在本机(mac book M5)完成,然后 rsync -avr 到 myvps2 /voxstudio/attendee 包括.env
4. 执行远端 VPS 部署操作前，先记得要从本机执行rsync -avr 代码和配置同步
5. 执行git commit push 时也需要执行rsync -avr 代码和配置同步到 myvps2 /voxstudio/attendee

6. 开发调试在本机 docker环境中，启动/关闭详见 ../voxella-docker-deploy/ related skill for local deployment:  /Users/adamwang/Project/subdub/voxella-docker-deploy/.cursor/skills/voxella-local-dev