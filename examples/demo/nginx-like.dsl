# nginx-like.dsl — mydsl 示例配置（nginx-like DSL）
# 行号固定，供 diff 行号定位演示与单元测试使用（第 1 行为注释）
server {
    listen 8080;
    server_name example.com www.example.com;
    root /var/www/example;

    location /api {
        proxy_pass http://backend:9000;
        proxy_read_timeout 30s;
    }

    location /static {
        alias /var/www/static;
    }
}

server {
    listen 9090;
    server_name admin.example.com;
}
