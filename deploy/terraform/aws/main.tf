resource "aws_ecs_cluster" "this" {
  name = "clhear"
}

resource "aws_iam_role" "execution" {
  name = "clhear-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_ecs_task_definition" "this" {
  family                   = "clhear"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn

  container_definitions = jsonencode([{
    name                   = "clhear"
    image                  = var.image_digest
    essential              = true
    command                = var.command
    user                   = "10001"
    readonlyRootFilesystem = true
    linuxParameters        = { initProcessEnabled = true }
    portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    environment = [
      { name = "CLHEAR_BIND_HOST", value = "0.0.0.0" },
      { name = "DATABASE_URL", value = "sqlite:////tmp/clhear.db" },
      { name = "CLHEAR_SCOPES_DIR", value = "/tmp/scopes" },
    ]
    secrets = var.service_token_secret_arn == "" ? [] : [{
      name      = "CLHEAR_SERVICE_TOKENS"
      valueFrom = var.service_token_secret_arn
    }]
  }])

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_service" "this" {
  name            = "clhear"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = var.assign_public_ip
  }
}
